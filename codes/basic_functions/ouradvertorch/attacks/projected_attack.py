from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import copy

import numpy as np
import torch
import torch.nn as nn

from random import randint

from torchvision import transforms
from PIL import Image

from ..utils import clamp, normalize_by_pnorm, rand_init_delta
from .interaction_loss import (InteractionLoss, get_features,
                               sample_for_interaction)
from codes.utils.util_linbp import linbp_forw_resnet50, linbp_backw_resnet50,ila_forw_resnet50,ILAProjLoss

if torch.cuda.is_available():
    device = torch.device('cuda')
else:
    device = torch.device('cpu')

def gkern(kernlen=21, nsig=3):
    """Returns a 2D Gaussian kernel array."""
    import scipy.stats as st

    x = np.linspace(-nsig, nsig, kernlen)
    kern1d = st.norm.pdf(x)
    kernel_raw = np.outer(kern1d, kern1d)
    kernel = kernel_raw / kernel_raw.sum()
    return kernel


def transition_invariant_conv(size=15):
    kernel = gkern(size, 3).astype(np.float32)
    padding = size // 2
    stack_kernel = np.stack([kernel, kernel, kernel])
    stack_kernel = np.expand_dims(stack_kernel, 1)

    conv = nn.Conv2d(
        in_channels=3,
        out_channels=3,
        kernel_size=size,
        stride=1,
        groups=3,
        padding=padding,
        bias=False)
    conv.weight.data = conv.weight.new_tensor(data=stack_kernel)

    return conv


def input_diversity(input_tensor,image_width,image_resize,prob):
    if prob > 0.0:
        rnd = randint(image_width,image_resize)
        rescaled = transforms.Resize([rnd, rnd],interpolation=Image.NEAREST)(input_tensor)
        h_rem = image_resize - rnd
        w_rem = image_resize - rnd
        pad_top = randint(0, h_rem)
        pad_bottom = h_rem - pad_top
        pad_left = randint(0, w_rem)
        pad_right = w_rem - pad_left
        # 要看一下padded的维度来验证  left, top, right and bottom
        padded = transforms.Pad([pad_left, pad_top,pad_right, pad_bottom])(rescaled)

        # padded.set_shape((input_tensor.shape[0], image_resize, image_resize, 3))
        rnd_prob = randint(0,100)/100.0
        if rnd_prob < prob:
            return padded
        else:
            return input_tensor
    else:
        return input_tensor

class ProjectionAttacker(object):

    def __init__(self,
                 attack_method,
                 model,
                 epsilon,
                 num_steps,
                 step_size,
                 linbp_layer,
                 ila_layer,
                 ila_niters,
                 ord='inf',
                 image_width=224,
                 loss_fn=None,
                 targeted=False,
                 grid_scale=8,
                 sample_times=32,
                 sample_grid_num=32,
                 momentum=0.0,
                 ti_size=1,
                 lam=1,
                 m=0,
                 sigma=15,
                 image_resize = 255,
                 prob = 0.0,
                 rand_init=True):
        self.attack_method = attack_method
        self.model = model
        self.epsilon = epsilon
        self.num_steps = num_steps
        self.step_size = step_size
        self.linbp_layer = linbp_layer
        self.ila_layer = ila_layer
        self.ila_niters = ila_niters
        self.image_width = image_width
        self.momentum = momentum
        self.targeted = targeted
        self.ti_size = ti_size
        self.lam = lam
        self.grid_scale = grid_scale
        self.sample_times = sample_times
        if self.ti_size > 1:
            self.ti_conv = transition_invariant_conv(ti_size)
        self.sample_grid_num = sample_grid_num
        self.m = m
        self.sigma = sigma
        self.ord = ord
        self.image_resize = image_resize
        self.prob = prob
        self.rand_init = rand_init
        if loss_fn is None:
            self.loss_fn = nn.CrossEntropyLoss()
        else:
            self.loss_fn = loss_fn

    def perturb(self, X, y):
        """
        :param X_nat: a Float Tensor  1,c,h,w  float32
        :param y: a Long Tensor 1 int64
        :return:
        """
        print(self.model)
        loss_record = {'loss1': [], 'loss2': [], 'loss': []}
        delta = torch.zeros_like(X)
        if self.rand_init and self.lam == 0:
            rand_init_delta(delta, X, self.ord, self.epsilon, 0.0, 1.0)
            delta.data = clamp(X + delta.data, min=0.0, max=1.0) - X

        delta.requires_grad_()

        grad = torch.zeros_like(X)
        deltas = torch.zeros_like(X).repeat(self.num_steps, 1, 1, 1)
        label = y.item()

        noise_distribution = torch.distributions.normal.Normal(
                    torch.tensor([0.0]),
                    torch.tensor([self.sigma]).float())
        # X_prev = X

        # # DIM attack
        # if self.prob >0:
        #     X = input_diversity(X, self.image_width, self.image_resize, self.prob)

        for i in range(self.num_steps):
            # # DI2 attack
            X_prev = X + delta
            X_DIM = input_diversity(X_prev, self.image_width, self.image_resize, self.prob)

            if self.m >= 1:  # Variance-reduced attack; https://arxiv.org/abs/1802.09707
                noise_shape = list(X_DIM.shape)
                noise_shape[0] = self.m
                noise = noise_distribution.sample(noise_shape).squeeze() / 255
                noise = noise.to(X_DIM.device)
                outputs = self.model(X_DIM + noise)

                loss1 = self.loss_fn(outputs, y.expand(self.m))
            else:
                loss1 = self.loss_fn(self.model(X_DIM), y)

            if self.targeted:
                loss1 = -loss1

            if self.lam > 0:  # Interaction-reduced attack
                only_add_one_perturbation, leave_one_out_perturbation = \
                    sample_for_interaction(delta, self.sample_grid_num,
                                           self.grid_scale, self.image_width,
                                           self.sample_times)

                (outputs, leave_one_outputs, only_add_one_outputs,
                 zero_outputs) = get_features(self.model, X, delta,
                                              leave_one_out_perturbation,
                                              only_add_one_perturbation)

                outputs_c = copy.deepcopy(outputs.detach())
                outputs_c[:, label] = -np.inf
                other_max = outputs_c.max(1)[1].item()
                interaction_loss = InteractionLoss(
                    target=other_max, label=label)
                average_pairwise_interaction = interaction_loss(
                    outputs, leave_one_outputs, only_add_one_outputs,
                    zero_outputs)

                if self.lam == float('inf'):
                    loss2 = -average_pairwise_interaction
                    loss = loss2
                else:
                    loss2 = -self.lam * average_pairwise_interaction
                    loss = loss1 + loss2

                loss_record['loss1'].append(loss1.item())
                loss_record['loss2'].append(
                    loss2.item() if self.lam > 0 else 0)
                loss_record['loss'].append(loss.item())
            else:
                loss = loss1
            loss.backward()

            deltas[i, :, :, :] = delta.data

            cur_grad = delta.grad.data
            if self.ti_size > 1:  # TI Attack; https://arxiv.org/abs/1904.02884
                self.ti_conv.to(X.device)
                cur_grad = self.ti_conv(cur_grad)

            # MI Attack; https://arxiv.org/abs/1710.06081
            cur_grad = normalize_by_pnorm(cur_grad, p=1)
            grad = self.momentum * grad + cur_grad

            if self.ord == np.inf:
                delta.data += self.step_size * grad.sign()
                delta.data = clamp(delta.data, -self.epsilon, self.epsilon)
                delta.data = clamp(X.data + delta.data, 0.0, 1.0) - X.data
            elif self.ord == 2:
                delta.data += self.step_size * normalize_by_pnorm(grad, p=2)
                delta.data *= clamp(
                    (self.epsilon * normalize_by_pnorm(delta.data, p=2) /
                     delta.data),
                    max=1.)
                delta.data = clamp(X.data + delta.data, 0.0, 1.0) - X.data
            else:
                error = "Only ord = inf and ord = 2 have been implemented"
                raise NotImplementedError(error)

            delta.grad.data.zero_()
        rval = X.data + deltas
        return rval, loss_record

    def perturb_linbp_ila(self, X, y):
        """
        :param X_nat: a Float Tensor  1,c,h,w  float32
        :param y: a Long Tensor 1 int64
        :return:
        """
        #
        # model = self.model
        # model.eval()
        # model = nn.Sequential(
        #     Normalize(),
        #     model
        # )
        # model.to(device)
        loss_record = {'loss1': [], 'loss2': [], 'loss': []}

        delta = torch.zeros_like(X)
        if self.rand_init and self.lam == 0:
            rand_init_delta(delta, X, self.ord, self.epsilon, 0.0, 1.0)
            delta.data = clamp(X + delta.data, min=0.0, max=1.0) - X

        X_adv = X + delta
        X_adv.requires_grad_()

        grad = torch.zeros_like(X)
        advs = torch.zeros_like(X).repeat(self.num_steps, 1, 1, 1)
        advs_ila = torch.zeros_like(X).repeat(self.ila_niters, 1, 1, 1)

        noise_distribution = torch.distributions.normal.Normal(
                    torch.tensor([0.0]),
                    torch.tensor([self.sigma]).float())
        # X_prev = X

        # # DIM attack
        # if self.prob >0:
        #     X = input_diversity(X, self.image_width, self.image_resize, self.prob)

        for i in range(self.num_steps):
            # # DI2 attack
            X_DIM = input_diversity(X_adv, self.image_width, self.image_resize, self.prob)
            X_DIM.requires_grad_()
            if self.m >= 1:  # Variance-reduced attack; https://arxiv.org/abs/1802.09707
                noise_shape = list(X_DIM.shape)
                noise_shape[0] = self.m
                noise = noise_distribution.sample(noise_shape).squeeze() / 255
                noise = noise.to(X_DIM.device)
                X_DIM = X_DIM + noise

            if 'linbp' in self.attack_method:
                att_out, ori_mask_ls, conv_out_ls, relu_out_ls, conv_input_ls = linbp_forw_resnet50(self.model, X_DIM, True,
                                                                                                    self.linbp_layer)
                pred = torch.argmax(att_out, dim=1).view(-1)
                loss1 = nn.CrossEntropyLoss()(att_out, y)
                self.model.zero_grad()
                cur_grad = linbp_backw_resnet50(X_adv, loss1, conv_out_ls, ori_mask_ls, relu_out_ls, conv_input_ls,
                                                  xp=1.)
            else:
                att_out = self.model(X_DIM)
                # pred = torch.argmax(att_out, dim=1).view(-1)
                loss1 = nn.CrossEntropyLoss()(att_out, y)
                self.model.zero_grad()
                loss1.backward()
                cur_grad = X_adv.grad.data
            self.model.zero_grad()

            advs[i, :, :, :] = X_adv.data

            # cur_grad = delta.grad.data
            if self.ti_size > 1:  # TI Attack; https://arxiv.org/abs/1904.02884
                self.ti_conv.to(X.device)
                cur_grad = self.ti_conv(cur_grad)

            # MI Attack; https://arxiv.org/abs/1710.06081
            cur_grad = normalize_by_pnorm(cur_grad, p=1)
            grad = self.momentum * grad + cur_grad

            if self.ord == np.inf:
                X_adv.data += self.step_size * grad.sign()
                X_adv.data = clamp(X_adv.data, X-self.epsilon, X+self.epsilon)
                X_adv.data = clamp(X_adv, 0.0, 1.0)
            elif self.ord == 2:
                X_adv.data += self.step_size * normalize_by_pnorm(grad, p=2)
                X_adv.data *= clamp(
                    (self.epsilon * normalize_by_pnorm(X_adv.data, p=2) /
                     X_adv.data),
                    max=1.)
                X_adv.data = clamp(X_adv.data, 0.0, 1.0)
            else:
                error = "Only ord = inf and ord = 2 have been implemented"
                raise NotImplementedError(error)

        rval = advs

        if 'ila' in self.attack_method:
            attack_img = X_adv.clone()
            X_adv = X.clone().to(device)
            with torch.no_grad():
                mid_output = ila_forw_resnet50(self.model, X, self.ila_layer)
                mid_original = torch.zeros(mid_output.size()).to(device)
                mid_original.copy_(mid_output)
                mid_output = ila_forw_resnet50(self.model, attack_img, self.ila_layer)
                mid_attack_original = torch.zeros(mid_output.size()).to(device)
                mid_attack_original.copy_(mid_output)
            for _ in range(self.ila_niters):
                X_adv.requires_grad_(True)
                mid_output = ila_forw_resnet50(self.model, X_adv, self.ila_layer)
                loss = ILAProjLoss()(
                    mid_attack_original.detach(), mid_output, mid_original.detach(), 1.0
                )
                self.model.zero_grad()
                loss.backward()
                grad = X_adv.grad.data
                self.model.zero_grad()

                advs_ila[i, :, :, :] = X_adv.data

                # # cur_grad = delta.grad.data
                # if self.ti_size > 1:  # TI Attack; https://arxiv.org/abs/1904.02884
                #     self.ti_conv.to(X.device)
                #     cur_grad = self.ti_conv(cur_grad)
                #
                # # MI Attack; https://arxiv.org/abs/1710.06081
                # cur_grad = normalize_by_pnorm(cur_grad, p=1)
                # grad = self.momentum * grad + cur_grad
                if self.ord == np.inf:
                    X_adv.data += self.step_size * grad.sign()
                    X_adv.data = clamp(X_adv.data, X - self.epsilon, X + self.epsilon)
                    X_adv.data = clamp(X_adv, 0.0, 1.0)
                elif self.ord == 2:
                    X_adv.data += self.step_size * normalize_by_pnorm(grad, p=2)
                    X_adv.data *= clamp(
                        (self.epsilon * normalize_by_pnorm(X_adv.data, p=2) /
                         X_adv.data),
                        max=1.)
                    X_adv.data = clamp(X_adv.data, 0.0, 1.0)
                else:
                    error = "Only ord = inf and ord = 2 have been implemented"
                    raise NotImplementedError(error)

            rval = advs_ila

            del mid_output, mid_original, mid_attack_original

            # X_adv.grad.data.zero_()
        # rval = advs
        return rval, loss_record
