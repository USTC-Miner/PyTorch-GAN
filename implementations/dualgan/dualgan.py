import argparse
import os
import numpy as np
import math
import itertools
import scipy

import torchvision.transforms as transforms
from torchvision.utils import save_image

from torch.utils.data import DataLoader
from torchvision import datasets
from torch.autograd import Variable
import torch.autograd as autograd

from datasets import *
from models import *

import torch.nn as nn
import torch.nn.functional as F
import torch

os.makedirs('images', exist_ok=True)

parser = argparse.ArgumentParser()
parser.add_argument('--n_epochs', type=int, default=200, help='number of epochs of training')
parser.add_argument('--batch_size', type=int, default=1, help='size of the batches')
parser.add_argument('--dataset_name', type=str, default='edges2shoes', help='name of the dataset')
parser.add_argument('--lr', type=float, default=0.0002, help='adam: learning rate')
parser.add_argument('--b1', type=float, default=0.5, help='adam: decay of first order momentum of gradient')
parser.add_argument('--b2', type=float, default=0.999, help='adam: decay of first order momentum of gradient')
parser.add_argument('--n_cpu', type=int, default=8, help='number of cpu threads to use during batch generation')
parser.add_argument('--img_size', type=int, default=128, help='size of each image dimension')
parser.add_argument('--channels', type=int, default=3, help='number of image channels')
parser.add_argument('--n_critic', type=int, default=5, help='number of training steps for discriminator per iter')
parser.add_argument('--sample_interval', type=int, default=200, help='interval betwen image samples')
opt = parser.parse_args()
print(opt)

img_shape = (opt.channels, opt.img_size, opt.img_size)

# Calculate output of image discriminator (PatchGAN)
patch = int(opt.img_size / 2**4)
patch = (1, patch, patch)

cuda = True if torch.cuda.is_available() else False

def weights_init_normal(m):
    classname = m.__class__.__name__
    if classname.find('Linear') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)

# Loss function
cycle_loss = torch.nn.L1Loss()

# Loss weights
lambda_adv = 1
lambda_cycle = 100
lambda_gp = 10

# Initialize generator and discriminator
G_AB = Generator()
G_BA = Generator()
D_A = Discriminator()
D_B = Discriminator()

if cuda:
    G_AB.cuda()
    G_BA.cuda()
    D_A.cuda()
    D_B.cuda()
    cycle_loss.cuda()

# Initialize weights
G_AB.apply(weights_init_normal)
G_BA.apply(weights_init_normal)
D_A.apply(weights_init_normal)
D_B.apply(weights_init_normal)

# Configure data loader
transforms_ = [ transforms.Resize((opt.img_size, opt.img_size*2), Image.BICUBIC),
                transforms.ToTensor(),
                transforms.Normalize((0.5,0.5,0.5), (0.5,0.5,0.5)) ]
dataloader = DataLoader(ImageDataset("../../data/%s" % opt.dataset_name, transforms_=transforms_),
                        batch_size=opt.batch_size, shuffle=True, num_workers=opt.n_cpu)

# Optimizers
optimizer_G = torch.optim.Adam( itertools.chain(G_AB.parameters(), G_BA.parameters()),
                                lr=opt.lr, betas=(opt.b1, opt.b2))
optimizer_D_A = torch.optim.Adam(D_A.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))
optimizer_D_B = torch.optim.Adam(D_B.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))

FloatTensor = torch.cuda.FloatTensor if cuda else torch.FloatTensor
LongTensor = torch.cuda.LongTensor if cuda else torch.LongTensor

def compute_gradient_penalty(D, real_samples, fake_samples):
    """Calculates the gradient penalty loss for WGAN GP"""
    # Random weight term for interpolation between real and fake samples
    alpha = FloatTensor(np.random.random((real_samples.size(0), 1, 1, 1)))

    # Get random interpolation between real and fake samples
    interpolates = alpha * real_samples + ((1 - alpha) * fake_samples)

    interpolates = Variable(interpolates, requires_grad=True)

    d_interpolates = D(interpolates)

    fake = Variable(FloatTensor(real_samples.shape[0], *patch).fill_(1.0), requires_grad=False)

    # Get gradient w.r.t. interpolates
    gradients = autograd.grad(outputs=d_interpolates, inputs=interpolates,
                              grad_outputs=fake, create_graph=True, retain_graph=True,
                              only_inputs=True)[0]

    gradient_penalty = lambda_gp * ((gradients.norm(2, dim=1) - 1) ** 2).mean()
    return gradient_penalty

# ----------
#  Training
# ----------

# Keeps the 5 latest image samples
image_samples = []

for epoch in range(opt.n_epochs):

    # Batch iterator
    data_iterator = iter(dataloader)

    for i in range(len(data_iterator) // opt.n_critic):
        # Train discriminator for n_critic times
        for _ in range(opt.n_critic):
            optimizer_G.zero_grad()

            batch = data_iterator.next()

            batch_size = batch['A'].size(0)

            # Adversarial ground truths
            valid = Variable(FloatTensor(batch_size, *patch).fill_(-1.0), requires_grad=False)
            fake = Variable(FloatTensor(batch_size, *patch).fill_(1.0), requires_grad=False)

            # Configure input
            imgs_A = Variable(batch['A'].type(FloatTensor))
            imgs_B = Variable(batch['B'].type(FloatTensor))

            # ----------------------
            #  Train Discriminators
            # ----------------------

            optimizer_D_A.zero_grad()
            optimizer_D_B.zero_grad()

            # Generate a batch of images
            fake_A = G_BA(imgs_B)
            fake_B = G_AB(imgs_A)

            #----------
            # Domain A
            #----------

            # Discriminate between real and fake samples
            real_validity_A = D_A(imgs_A)
            real_validity_A.backward(valid)
            fake_validity_A = D_A(fake_A)
            fake_validity_A.backward(fake)

            # Compute gradient penalty for improved wasserstein training
            gp_A = compute_gradient_penalty(D_A, imgs_A.data, fake_A.data)
            gp_A.backward()

            #----------
            # Domain B
            #----------

            # Discriminate between real and fake samples
            real_validity_B = D_B(imgs_B)
            real_validity_B.backward(valid)
            fake_validity_B = D_B(fake_B)
            fake_validity_B.backward(fake)

            # Compute gradient penalty for improved wasserstein training
            gp_B = compute_gradient_penalty(D_B, imgs_B.data, fake_B.data)
            gp_B.backward()

            # Total adversarial loss
            D_A_loss = real_validity_A - fake_validity_A
            D_B_loss = real_validity_B - fake_validity_B

            optimizer_D_A.step()
            optimizer_D_B.step()

        # ------------------
        #  Train Generators
        # ------------------

        optimizer_G.zero_grad()

        # Translate images to opposite domain
        fake_A = G_BA(imgs_B)
        fake_B = G_AB(imgs_A)

        # Reconstruct images
        recov_A = G_BA(fake_B)
        recov_B = G_AB(fake_A)

        # Adversarial loss
        validity_A = (lambda_adv / 2) * D_A(fake_A)
        validity_A.backward(valid)
        validity_B = (lambda_adv / 2) * D_B(fake_B)
        validity_B.backward(valid)

        # Cycle-consistency loss
        cycle_A = (lambda_cycle / 2) * cycle_loss(recov_A, imgs_A)
        cycle_A.backward()
        cycle_B = (lambda_cycle / 2) * cycle_loss(recov_B, imgs_B)
        cycle_B.backward()

        optimizer_G.step()

        # Total losses
        G_adv = validity_A + validity_B
        G_cycle = cycle_A + cycle_B

        #--------------
        # Log Progress
        #--------------

        print ("[Epoch %d/%d] [Batch %d/%d] [D_A loss: %f] [D_B loss: %f] [G loss: %f, cycle: %f]" % (epoch, opt.n_epochs,
                                                        i * opt.n_critic, len(dataloader),
                                                        D_A_loss.data.cpu().numpy()[0].mean(),
                                                        D_B_loss.data.cpu().numpy()[0].mean(),
                                                        G_adv.data.cpu().numpy()[0].mean(), G_cycle.data[0]))


        # Create image sample
        ABA = torch.cat((imgs_A.data, fake_B.data, recov_A.data), -2)
        BAB = torch.cat((imgs_B.data, fake_A.data, recov_B.data), -2)
        sample = torch.cat((ABA, BAB), -2)
        image_samples.append(sample)
        if len(image_samples) > 5:
            image_samples.pop(0)

        # Check sample interval => save sample if there
        batches_done = len(dataloader) * epoch + i * opt.n_critic
        if batches_done % opt.sample_interval == 0:
            save_image(torch.cat(image_samples, -1), 'images/%d.png' % batches_done, nrow=2, normalize=True)
