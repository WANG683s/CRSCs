import torch
torch.set_printoptions(10)

import torch.nn.functional as F
import torch.nn as nn
import numpy as np
import torch.optim as optim
import os
import time
import math
from thop import profile


from skimage.feature import local_binary_pattern


import shutil
from matplotlib import pyplot as plt

from models.squid import AE
from models.memory import MemoryQueue

import random
import importlib

from tqdm import tqdm

from tools import parse_args, build_disc, log, log_loss, save_image, backup_files
from alert import GanAlert


args = parse_args()

CONFIG = importlib.import_module('configs.'+args.config).Config()

if not os.path.exists(os.path.join('checkpoints', args.exp)):
    os.mkdir(os.path.join('checkpoints', args.exp))

if not os.path.exists(os.path.join('checkpoints', args.exp, 'test_images')):
    os.mkdir(os.path.join('checkpoints', args.exp, 'test_images'))

save_path = os.path.join('checkpoints', args.exp, 'test_images')

# log
log_file = open(os.path.join('checkpoints', args.exp, 'log.txt'), 'w')

# backup files
backup_files(args)

# main AE
model = AE(1, 32, CONFIG.shrink_thres, num_slots=CONFIG.num_slots, num_patch=CONFIG.num_patch, level=CONFIG.level, 
            ratio=CONFIG.mask_ratio, initial_combine=CONFIG.initial_combine, drop=CONFIG.drop,
            dist=CONFIG.dist, memory_channel=CONFIG.memory_channel, mem_num_slots=CONFIG.mem_num_slots,
            ops=CONFIG.ops, decoder_memory=CONFIG.decoder_memory).cuda()


opt = CONFIG.opt(model.parameters(), lr=CONFIG.lr, eps=1e-7, betas=(0.5, 0.999), weight_decay=0.00001)
scheduler = CONFIG.scheduler(opt, **CONFIG.scheduler_args)





# for discriminator
if (CONFIG.enbale_gan is not None and CONFIG.enbale_gan >= 0):
    discriminator = build_disc(CONFIG)
    opt_d = CONFIG.opt(discriminator.parameters(), betas=(0.5, 0.999), lr=CONFIG.gan_lr)
    # scheduler_d = CONFIG.scheduler_d(opt_d, **CONFIG.scheduler_args_d)


# criterions
ce = nn.BCEWithLogitsLoss().cuda()
recon_criterion = torch.nn.MSELoss(reduction='mean').cuda()

kl = nn.KLDivLoss().cuda()

# alert
alert = GanAlert(discriminator=discriminator, args=args, CONFIG=CONFIG, generator=model)


def main():

    best_auc = -1

    for epoch in range(CONFIG.epochs):

        a=time.time()
        
        # when GAN training is disabled
        if CONFIG.enbale_gan is None or epoch < CONFIG.enbale_gan:
            train_loss = train(CONFIG.train_loader, epoch)
            val_loss = {'recon_l1': 0.}
            log_loss(log_file, epoch, train_loss, val_loss)
            continue
        
        # when GAN training is enabled 
        train_loss = gan_train(CONFIG.train_loader, epoch)
        
        reconstructed, inputs, scores, labels, val_loss = val(CONFIG.val_loader, epoch)
        
        log_loss(log_file, epoch, train_loss, val_loss)

        # do we need scheduler for discriminator?
        scheduler.step()      

        # alert, collect=true uses train set mean/std
        results = alert.evaluate(scores, labels, collect=True)
        
        # log metrics
        msg = '[VAL metrics] '
        for k, v in results.items():
            msg += k + ': '
            msg += '%.2f ' % v
        log(log_file, msg)
        
        # save best model
        if results['auc'] > best_auc - 0.5: # a little bit tolerance
            if results['auc'] > best_auc:
                best_auc = results['auc']
            save_image(os.path.join(save_path, 'best'), zip(reconstructed, inputs))
            if CONFIG.enbale_gan is not None:
                torch.save(discriminator.state_dict(), os.path.join('checkpoints',args.exp,'discriminator.pth'))
            torch.save(model.state_dict(), os.path.join('checkpoints',args.exp,'model.pth'))
            log(log_file, 'save model!')

        # save latest model
        if CONFIG.enbale_gan is not None:
            torch.save(discriminator.state_dict(), os.path.join('checkpoints',args.exp,'discriminator_latest.pth'))
        torch.save(model.state_dict(), os.path.join('checkpoints',args.exp,'model_latest.pth'))

        # save last 10 epochs generated imgs for debugging
        if epoch >= CONFIG.epochs - 10:
            save_image(os.path.join(save_path, 'epoch_'+str(epoch)), zip(reconstructed, inputs))
        
        save_image(os.path.join(save_path, 'latest'), zip(reconstructed, inputs))

        
    log_file.close()


def train(dataloader, epoch):
    model.train()
    batches_done = 0
    tot_loss = {'recon_loss': 0., 'g_loss': 0., 'd_loss': 0., 't_recon_loss': 0., 'dist_loss': 0.}
    
    # clip dataloader
    if CONFIG.limit is None:
        limit = len(dataloader) - len(dataloader) % CONFIG.n_critic
    else:
        limit = CONFIG.limit

    for i, (img, label) in enumerate(tqdm(dataloader, disable=CONFIG.disable_tqdm)):
        if i > limit:
            break
        batches_done += 1

        img = img.to(CONFIG.device)
        label = label.to(CONFIG.device)
        
        opt.zero_grad()
        
        out = model(img)

        if CONFIG.alert is not None:
            CONFIG.alert.record(out['recon'].detach(), img)

        loss_all = CONFIG.recon_w * recon_criterion(out["recon"], img)

        loss_all += 0.05 * tsim(out["recon"], img)
        
        tot_loss['recon_loss'] += loss_all.item()

        if CONFIG.dist and 'teacher_recon' in out and torch.is_tensor(out['teacher_recon']):
            t_recon_loss = CONFIG.t_w * recon_criterion(out["teacher_recon"], img)

            t_recon_loss += tsim(out["teacher_recon"], img)
            
            loss_all =  loss_all + t_recon_loss
            tot_loss['t_recon_loss'] += t_recon_loss.item()

        if  CONFIG.dist and 'dist_loss' in out and torch.is_tensor(out['dist_loss']):
            dist_loss = CONFIG.dist_w  * out["dist_loss"]
            loss_all = loss_all + dist_loss
            tot_loss['dist_loss'] += dist_loss.item()

        loss_all.backward()
        opt.step()

        for module in model.modules():
            if isinstance(module, MemoryQueue):
                module.update()

    # avg loss
    for k, v in tot_loss.items():
        tot_loss[k] /= batches_done

    return tot_loss

def gan_train(dataloader, epoch):
    model.train()
    batches_done = 0
    tot_loss = {'loss': 0., 'recon_loss': 0., 'g_loss': 0., 'd_loss': 0., 't_recon_loss': 0., 'dist_loss': 0.}

    # clip dataloader
    if CONFIG.limit is None:
        limit = len(dataloader) - len(dataloader) % CONFIG.n_critic
    else:
        limit = CONFIG.limit

    for i, (img, label) in enumerate(tqdm(dataloader, disable=CONFIG.disable_tqdm)):
        if i > limit:
            break
        batches_done += 1

        img = img.to(CONFIG.device)
        label = label.to(CONFIG.device)

        d_loss = train_discriminator(img)
        tot_loss['d_loss'] += d_loss

        # train generator at every n_critic step only
        if i % CONFIG.n_critic == 0:

            opt.zero_grad()
            
            out = model(img)

            if CONFIG.alert is not None:
                CONFIG.alert.record(out['recon'].detach(), img)
            
            # reconstruction loss
            recon_loss = CONFIG.recon_w * recon_criterion(out["recon"], img)
            tot_loss['recon_loss'] += recon_loss.item()
            loss_all = recon_loss
            
             loss_all += tsim(out["recon"], img)

            # generator loss
            fake_validity = discriminator(out["recon"])
                
            g_loss = CONFIG.g_w * ce(fake_validity, torch.ones_like(fake_validity))
  
            tot_loss['g_loss'] += g_loss.item()
            loss_all = loss_all + g_loss

            # teacher decoder loss
            if  CONFIG.dist and 'teacher_recon' in out and torch.is_tensor(out['teacher_recon']):
                t_recon_loss = CONFIG.t_w * recon_criterion(out["teacher_recon"], img)
                tot_loss['t_recon_loss'] += t_recon_loss.item()
                loss_all = loss_all + t_recon_loss

            # distillation loss
            if  CONFIG.dist and 'dist_loss' in out and torch.is_tensor(out['dist_loss']):
                dist_loss = CONFIG.dist_w * out["dist_loss"]
                tot_loss['dist_loss'] += dist_loss.item()
                loss_all = loss_all + dist_loss

            tot_loss['loss'] += loss_all.item()

            loss_all.backward()
            opt.step()

            for module in model.modules():
                if isinstance(module, MemoryQueue):
                    module.update()

    # avg loss
    for k, v in tot_loss.items():
        tot_loss[k] /= batches_done

    return tot_loss



def tsim(img1, img2, window_size=11, size_average=True):

    # 构建高斯权重矩阵
    channel = img1.size()[1]
    window = torch.Tensor(_ssim_window(window_size, channel)).to(img1.device)
    window = window.unsqueeze(0).unsqueeze(1)

    # 计算均值、方差和协方差
    mu1 = F.conv2d(img1, window, padding=window_size // 2, groups=channel)
    mu2 = F.conv2d(img2, window, padding=window_size // 2, groups=channel)
    mu1_sq = mu1.pow(2)
    mu2_sq = mu2.pow(2)
    mu1_mu2 = mu1 * mu2

    sigma1_sq = F.conv2d(img1 * img1, window, padding=window_size // 2, groups=channel) - mu1_sq
    sigma2_sq = F.conv2d(img2 * img2, window, padding=window_size // 2, groups=channel) - mu2_sq
    sigma12 = F.conv2d(img1 * img2, window, padding=window_size // 2, groups=channel) - mu1_mu2

    C1 = (0.01 ** 2)
    C2 = (0.03 ** 2)
    ssim_map = ((2 * mu1_mu2 + C1) * (2 * sigma12 + C2)) / ((mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2))+ texture_consistency_loss(img1,img2)


    if size_average:
        return torch.mean((1.0 - ssim_map) / 2)
    else:
        return torch.sum((1.0 - ssim_map) / 2)


def _ssim_window(window_size, channel):

    sigma = 1.5
    gauss = torch.Tensor([math.exp(-(x - window_size // 2) ** 2 / float(2 * sigma ** 2)) for x in range(window_size)])
    gauss = gauss / gauss.sum()

    window_1d = gauss.unsqueeze(1)
    window_2d = window_1d.mm(window_1d.t()).float().unsqueeze(0).unsqueeze(0)
    window_2d = window_2d.expand(channel, 1, window_size, window_size).contiguous()

    return window_2d



def compute_lbp(image, P=8, R=1):

    lbp = local_binary_pattern(image, P, R, method='uniform')
    return lbp

def texture_consistency_loss(image1, image2):

    lbp1 = compute_lbp(image1)
    lbp2 = compute_lbp(image2)
    return np.mean(np.abs(lbp1 - lbp2))







def val(dataloader, epoch):

    aa = time.time()
    
    model.eval()
    tot_loss = {'recon_l1': 0.}
    
    # for reconstructed img
    reconstructed = []
    # for input img
    inputs = []
    # for anomaly score
    scores = []
    # for gt labels
    labels = []

    count = 0
    for i, (img, label) in enumerate(dataloader):
        count += img.shape[0]
        img = img.to(CONFIG.device)
        label = label.to(CONFIG.device)

        opt.zero_grad()

        out = model(img)
        fake_v = discriminator(out['recon'])

        scores += list(fake_v.detach().cpu().numpy())
        labels += list(label.detach().cpu().numpy())
        reconstructed += list(out['recon'].detach().cpu().numpy())
        inputs += list(img.detach().cpu().numpy())

        # this is just an indication
        tot_loss['recon_l1'] += torch.mean(torch.abs(out['recon'] - img)).item()

    tot_loss['recon_l1'] = tot_loss['recon_l1'] / count

    bb = time.time()
    print("========>",bb-aa)
    
    return reconstructed, inputs, scores, labels, tot_loss

def train_discriminator(img):
    opt_d.zero_grad()

    out = model(img)

    # Real images
    real_validity = discriminator(img)
    # Fake images
    fake_validity = discriminator(out["recon"].detach())

    # cross_entropy loss
    d_loss = ce(real_validity, torch.ones_like(real_validity))
    d_loss += ce(fake_validity, torch.zeros_like(fake_validity))
    d_loss *= CONFIG.d_w 

    d_loss.backward()
    opt_d.step()

    return d_loss.item()


if __name__ == '__main__':
    main()
