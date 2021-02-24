"""
1. Pretraining SimCLR & Proto-typing 
2. Training OOD (one-class classification)
3. Evaluation (eval.py?)
\
"""
import sys, os
import pandas as pd
import utils,json
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as tr
import torch.optim as optim, copy

from tqdm import tqdm
from sklearn.metrics import roc_auc_score
import model_csi as C
from dataloader_es import *
from parser import * 
import transform_layers as TL
import torch.optim.lr_scheduler as lr_scheduler
from soyclustering import SphericalKMeans
from scipy import sparse
from randaugment_without_rotation import *

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
hflip = TL.HorizontalFlipLayer().to(device)
import random,numpy as np
def set_random_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
### helper functions
def checkpoint(f,  tag, args, device):
    f.cpu()
    ckpt_dict = {
        "model_state_dict": f.state_dict(),
        "protos": f.module.prototypes
    }
    torch.save(ckpt_dict, os.path.join(args.save_dir, tag))
    f.to(device)

def energy_score(z, model):
    zp = model.module.prototypes
    logits = torch.matmul(z, zp.t()) / args.temperature
    Le = torch.log(torch.exp(logits).sum(dim=1))
    return Le

def cal_class_auroc(nd1,nd2,nd3,nd4,nd5,and1,and2,and3,and4,and5,ndsum,andsum,ndmul,andmul,cls_list):
    # Class AUROC
    normal_class = args.known_normal
    anomaly_classes = [i for i in range(args.n_classes)]
    anomaly_classes.remove(normal_class)
    
    tosum_average = 0
    tomul_average = 0
    tod1_average = 0
    tod2_average = 0
    tod3_average = 0
    tod4_average = 0
    tod5_average = 0
    for anomaly in anomaly_classes:
        tosum = ndsum + np.array(andsum)[np.array(cls_list) == anomaly].tolist()
        tomul = ndmul + np.array(andmul)[np.array(cls_list) == anomaly].tolist()
        tod1 = nd1 + np.array(and1)[np.array(cls_list) == anomaly].tolist()
        tod2 = nd2 + np.array(and2)[np.array(cls_list) == anomaly].tolist()
        tod3 = nd3 + np.array(and3)[np.array(cls_list) == anomaly].tolist()
        tod4 = nd4 + np.array(and4)[np.array(cls_list) == anomaly].tolist()
        tod5 = nd5 + np.array(and5)[np.array(cls_list) == anomaly].tolist()
        total_label = [1 for i in range(len(ndsum))] + [0 for i in range(len(tosum) - len(ndsum))]
        print('---------------------- Evaluation class: {} --------------------------'.format(anomaly))
        print(len(ndsum), len(tosum) - len(ndsum))
        print("sum\t", roc_auc_score(total_label, tosum))
        print("mul\t", roc_auc_score(total_label, tomul))
        print("px\t", roc_auc_score(total_label, tod1))
        print("pyx\t", roc_auc_score(total_label, tod2))
        print("pshi\t", roc_auc_score(total_label, tod3))
        print("pshienergy\t", roc_auc_score(total_label, tod4))
        print("pshiyx\t",roc_auc_score(total_label, tod5))
        print('----------------------------------------------------------------------')
        print()
        
        tosum_average += roc_auc_score(total_label, tosum)
        tomul_average += roc_auc_score(total_label, tomul)
        tod1_average  += roc_auc_score(total_label, tod1)
        tod2_average  += roc_auc_score(total_label, tod2)
        tod3_average  += roc_auc_score(total_label, tod3)
        tod4_average  += roc_auc_score(total_label, tod4)
        tod5_average  += roc_auc_score(total_label, tod5)
    
    tosum_average /= len(anomaly_classes)
    tomul_average /= len(anomaly_classes)
    tod1_average /= len(anomaly_classes)
    tod2_average /= len(anomaly_classes)
    tod3_average /= len(anomaly_classes)      
    tod4_average /= len(anomaly_classes)    
    tod5_average /= len(anomaly_classes)
    print('------------------- Evaluation class average --------------------')
    print(len(ndsum), len(tosum) - len(ndsum))
    print("sum\t", tosum_average)
    print("mul\t", tomul_average)
    print("px\t", tod1_average)
    print("pyx\t", tod2_average)
    print("pshi\t", tod3_average)
    print("pshienergy\t", tod4_average)
    print("pshiyx\t", tod5_average)
    print('----------------------------------------------------------------------')
    print()
    return 
def get_features(pos_1,model,use_simclr_aug=False,use_ensemble=False):
    if use_ensemble:
        sample_num = args.sample_num
    out_ensemble, pen_out_ensemble = [], []
    for seed in range(args.sample_num): # for ensemble run  N times
        set_random_seed(seed) # random seed setting
        images1 = torch.cat([rotation(hflip(pos_1), k) for k in range(4)]) # 4B
        if use_simclr_aug:
            images1 = simclr_aug(images1) 
        _, outputs_aux = model(images1, simclr=True, penultimate=True, shift=True)
        out = outputs_aux['simclr'] # 4B, D
#         norm_out = F.normalize(outputs_aux['simclr'],dim=-1) # 4B, D
        pen_out = outputs_aux['shift'] # 4B, D
                
        out_ensemble.append(out) 
                
        pen_out_ensemble.append(pen_out)
            ## ensembling 
    out = torch.stack(out_ensemble,dim=1).mean(dim=1) # N D
#     print(out.shape,torch.stack(out_ensemble,dim=1).shape)
    pen_out = torch.stack(pen_out_ensemble,dim=1).mean(dim=1) # N, 4
#     print(pen_out.shape, torch.stack(pen_out_ensemble,dim=1).shape)
    norm_out = F.normalize(out,dim=-1)
#     raise
    return out,pen_out,norm_out
def generate_prototypes(model, valid_loader, n_cluster=100, split = False):
    first = True
    with torch.no_grad():
        for idx, (pos_1, _, _, semi_target,_, _) in enumerate(valid_loader):
            pos_1 = pos_1.cuda(non_blocking=True) # B
            images1 = torch.cat([rotation(pos_1, k) for k in range(4)]) # 4B

            _, outputs_aux = model(images1, simclr=True, penultimate=True, shift=True)
            out = F.normalize(outputs_aux['simclr'],dim=-1)

            all_semi_target = semi_target.repeat(4)
            true_out = out[all_semi_target != -1,:]    
            true_out_list = torch.stack(true_out.chunk(4, dim = 0), dim = 1) # [B*D, B*D, B*D, B*D] -> B*4*D

            if first:
                all_out_list = true_out_list
                first = False
            else:
                all_out_list = torch.cat((all_out_list, true_out_list), dim = 0)

    # Set prototypes (k-means++)
    all_out_numpy = all_out_list.cpu().numpy() # T * 4 * D
    proto_list = []

    if split:
        for i in tqdm(range(4)):
            all_out_shi = sparse.csr_matrix(all_out_numpy[:, i, :])
            print(sum(np.isnan(all_out_numpy[:, i, :])))
            
            while True:
                try:
                    spherical_kmeans = SphericalKMeans(
                        n_clusters=n_cluster,
                        max_iter=10,
                        verbose=1,
                        init='similar_cut'
                    )

                    spherical_kmeans.fit(all_out_shi)
                    break
                except KeyboardInterrupt:
                    assert 0
                except:
                    print("K-means failure... Retrying")
                    continue
            protos = spherical_kmeans.cluster_centers_
            protos = F.normalize(torch.Tensor(protos), dim = -1)
            proto_list.append(protos.to(device))

        return proto_list
    else:
        all_out = all_out_numpy.reshape(-1, all_out_numpy.shape[2])
        all_out_sp = sparse.csr_matrix(all_out)
        print(sum(np.isnan(all_out)))
        
        while True:
            try:
                spherical_kmeans = SphericalKMeans(
                    n_clusters=n_cluster,
                    max_iter=10,
                    verbose=1,
                    init='similar_cut'
                )

                spherical_kmeans.fit(all_out_sp)
                break
            except KeyboardInterrupt:
                assert 0
            except:
                print("K-means failure... Retrying")
                continue
        
        protos = spherical_kmeans.cluster_centers_
        protos = F.normalize(torch.Tensor(protos), dim = -1)
        return protos.to(device)
def earlystop_score(model,valid_loader):
    rot_num = 4
    weighted_aucscores,aucscores = [],[]
    zp = model.module.prototypes
    for pos,pos2,_,semi_target,_,raw in valid_loader:
        prob,prob2, label_list = [] , [], []
        weighted_prob, weighted_prob2 = [], []
        Px_mean,Px_mean2 = 0, 0  
#         weighted_Px_mean, weighted_Px_mean2 = 0, 0

        images1 = torch.cat([rotation(pos, k) for k in range(rot_num)])
        images2 = torch.cat([rotation(pos2, k) for k in range(rot_num)])
        images1 = images1.to(device)
        images2 = images2.to(device)
        images1 = simclr_aug(images1)
        images2 = simclr_aug(images2)

    #     images = torch.cat([images1, images2], dim=0)  # 8B
        all_semi_targets = torch.cat([semi_target,semi_target+1])

        _, outputs_aux = model(images1, simclr=True, penultimate=True, shift=True)
        norm_out = F.normalize(outputs_aux['simclr'],dim=-1)
        pen_out = outputs_aux['shift']

        logits = torch.matmul(norm_out, zp.t()) # (B + B + B + B, # of P)
        logits_list = logits.chunk(rot_num, dim = 0) # list of (B, # of P)
        out_list = norm_out.chunk(rot_num, dim = 0)
        pen_out_list = pen_out.chunk(rot_num, dim = 0) # (B, 4)의 list
        for shi in range(rot_num):
            # Energy / Similar to P(x)
            Px_mean += torch.log(torch.exp(logits_list[shi]).sum(dim=1)) 
#             weighted_Px_mean +=torch.log(torch.exp(logits_list[shi]).sum(dim=1)) * all_weight_energy[shi]
        prob.extend(Px_mean.tolist())
#         weighted_prob.extend(weighted_Px_mean.tolist())
        
        _, outputs_aux = model(images2, simclr=True, penultimate=True, shift=True)
        norm_out = F.normalize(outputs_aux['simclr'],dim=-1)
        pen_out = outputs_aux['shift']

        logits = torch.matmul(norm_out, zp.t()) # (B + B + B + B, # of P)
        logits_list = logits.chunk(rot_num, dim = 0) # list of (B, # of P)
        out_list = norm_out.chunk(rot_num, dim = 0) 
        pen_out_list = pen_out.chunk(rot_num, dim = 0) # (B, 4)의 list
        for shi in range(rot_num):
            # Energy / Similar to P(x)
            Px_mean2 += torch.log(torch.exp(logits_list[shi]).sum(dim=1)) 
#             weighted_Px_mean2 +=torch.log(torch.exp(logits_list[shi]).sum(dim=1)) * all_weight_energy[shi]
        prob2.extend(Px_mean2.tolist())
#         weighted_prob2.extend(weighted_Px_mean2.tolist())

        label_list.extend(all_semi_targets)
        aucscores.append(roc_auc_score(label_list, prob2+prob))
#         weighted_aucscores.append(roc_auc_score(label_list, weighted_prob2+weighted_prob))
    print("earlystop_score:",np.mean(aucscores))
    return np.mean(aucscores)
#     print("weighted_score:",np.mean(weighted_aucscores))
def test(model, test_loader, train_loader, epoch):
    model.eval()
    with torch.no_grad():
        ndsum, ndmul, nd1, nd2, nd3, nd4,nd5 = [], [], [], [], [], [], []
        andsum, andmul, and1, and2, and3, and4,and5 = [], [], [], [], [], [], []
        cls_list = []
        first = True
        for idx, (pos_1, _, _, semi_target,_, _) in enumerate(train_loader):
            pos_1 = pos_1.cuda(non_blocking=True) # B
            semi_target = semi_target.to(device)
            out,pen_out,norm_out = get_features(pos_1,model,use_simclr_aug=True,use_ensemble=True) # outs = (4*B,D)
            # 
            all_semi_target = semi_target.repeat(4)
            true_out = out[all_semi_target != -1,:]    
            true_pen_out = pen_out[all_semi_target != -1,:]    
            true_pen_energy_out = torch.logsumexp(pen_out[all_semi_target != -1,:], dim = -1) # (4B)
            false_pen_energy_out = torch.logsumexp(pen_out[all_semi_target == -1,:], dim = -1) # (4B)
            
            a = F.softmax(pen_out[all_semi_target != -1,:], dim = -1) # B, 4
            b = torch.logsumexp(pen_out[all_semi_target != -1,:], dim = -1) # B
            true_pen_yx_out = ( b * a.t() ).t()
             
            
            
            true_out_list = torch.stack(true_out.chunk(4, dim = 0), dim = 1) # [B*D, B*D, B*D, B*D] -> B*4*D
            true_pen_out_list = torch.stack(true_pen_out.chunk(4, dim = 0), dim = 1)  # [B*D, B*D, B*D, B*D] -> B*4*D
            true_pen_energy_out_list = torch.stack(true_pen_energy_out.chunk(4, dim = 0), dim = 1) # 4B -> B * 4
            false_pen_energy_out_list = torch.stack(false_pen_energy_out.chunk(4, dim = 0), dim = 1) # 4B -> B * 4
            true_pen_yx_out_list = torch.stack(true_pen_yx_out.chunk(4, dim = 0), dim = 1) # [B*D, B*D, B*D, B*D] -> B*4*D

            if first:
                all_out_list = true_out_list
                all_pen_out_list = true_pen_out_list
                all_true_pen_energy_out_list = true_pen_energy_out_list
                all_true_pen_yx_out_list =  true_pen_yx_out_list
                all_false_pen_energy_out_list = false_pen_energy_out_list
                first = False
                print(true_pen_yx_out.shape)
            else:
                all_out_list = torch.cat((all_out_list, true_out_list), dim = 0)
                all_pen_out_list = torch.cat((all_pen_out_list, true_pen_out_list), dim = 0)
                all_true_pen_energy_out_list = torch.cat((all_true_pen_energy_out_list, true_pen_energy_out_list), dim = 0)
                all_true_pen_yx_out_list = torch.cat((all_true_pen_yx_out_list, true_pen_yx_out_list), dim = 0)
                all_false_pen_energy_out_list = torch.cat((all_false_pen_energy_out_list, false_pen_energy_out_list), dim = 0)
                
        all_axis = []
        for f in all_out_list.chunk(4, dim=1): # (B, 4, D) -> (B, 1, D)의 리스트
            axis = f.mean(dim=1)  # (B, 1, d) -> (B, d)
            all_axis.append(F.normalize(axis, dim=-1).to(args.device))
#             all_axis.append(axis.to(args.device))
                
        f_sim = [f.mean(dim=1) for f in all_out_list.chunk(4, dim=1)]  # list of (T, d) where T = total data size
        f_shi = [f.mean(dim=1) for f in all_pen_out_list.chunk(4, dim=1)]  # list of (T, 4)
        f_shi_energy = all_true_pen_energy_out_list.chunk(4, dim=1) # list of (T)
        f_shi_yx = [f.mean(dim=1) for f in all_true_pen_yx_out_list.chunk(4, dim=1)] # list of (T, 4)
            
        weight_sim = []
        weight_shi = []
        weight_energy = []
        weight_shi_energy = []
        weight_shi_yx = []
        zp = model.module.prototypes
        for shi in range(4):
            sim_norm = f_sim[shi].norm(dim=1)  # (T)
            shi_mean = f_shi[shi][:, shi]  # (T)
            shi_energy = f_shi_energy[shi] # (T)
            shi_yx = f_shi_yx[shi][:, shi]
            
            f_normalized = F.normalize(f_sim[shi], dim = 1)
            f_logits = torch.matmul(f_normalized, zp.t()) # (T, d) * (d, P) = (T, P)
#             f_logits = torch.matmul(sim_norm, zp.t()) # (T, d) * (d, P) = (T, P)
            f_energy = torch.log(torch.exp(f_logits).sum(dim=1))
            
            weight_sim.append(1 / sim_norm.mean().item())
            weight_shi.append(1 / shi_mean.mean().item())
            weight_shi_energy.append(1 / shi_energy.mean().item())
            weight_energy.append(1 / f_energy.mean().item())
            weight_shi_yx.append(1 / shi_yx.mean().item())
            
        all_weight_sim = weight_sim
        all_weight_shi = weight_shi
        all_weight_energy = weight_energy
        all_weight_shi_energy = weight_shi_energy
        all_weight_shi_yx = weight_shi_yx
        
        print(f'weight_sim:\t' + '\t'.join(map('{:.4f}'.format, all_weight_sim)))
        print(f'weight_shi:\t' + '\t'.join(map('{:.4f}'.format, all_weight_shi)))
        print(f'weight_energy:\t' + '\t'.join(map('{:.4f}'.format, all_weight_energy)))
        print(f'weight_shi_energy:\t' + '\t'.join(map('{:.4f}'.format, all_weight_shi_energy)))
        print(f'weight_shi_yx:\t' + '\t'.join(map('{:.4f}'.format, all_weight_shi_yx)))
        
        
        for idx, (pos_1, _, target,  _,cls,_) in enumerate(test_loader):
            
            negative_target = (target == 1).nonzero().squeeze()
            positive_target = (target != 1).nonzero().squeeze()
            
            pos_1 = pos_1.cuda(non_blocking=True) # B
            zp = model.module.prototypes
            
            out, pen_out, norm_out = get_features(pos_1,model,use_simclr_aug=True,use_ensemble=True)
            
            
            logits = torch.matmul(norm_out, zp.t()) # (B + B + B + B, # of P)
            logits_list = logits.chunk(4, dim = 0) # list of (B, # of P)
            out_list = norm_out.chunk(4, dim = 0) 
            pen_out_list = pen_out.chunk(4, dim = 0) # (B, 4)의 list
            
            Px_mean = 0
            Pygivenx_mean = 0
            Pshi_mean = 0
            Pshi_energy = 0
            Pshi_yx = 0
            for shi in range(4):
                # Energy / Similar to P(x)
                Px_mean += torch.log(torch.exp(logits_list[shi]).sum(dim=1)) * all_weight_energy[shi]

                # Similar to P(y|x)
                Pygivenx_mean += (torch.matmul(out_list[shi], all_axis[shi].t())).max(dim=1)[0] * all_weight_sim[shi]                
                
                # Energy from penultimate layer
                Pshi_mean += pen_out_list[shi][:, shi] * all_weight_shi[shi]    
                
                # Energy from shi
                pshi_x = torch.logsumexp(pen_out_list[shi], dim = 1)
                Pshi_energy += pshi_x * all_weight_shi_energy[shi]
                
                # Energy yx from shi
                pshi_ygivenx = F.softmax(pen_out_list[shi], dim = -1)[:, shi]
                # (pshi_x * pshi_ygivenx.t()).t() 
                Pshi_yx += pshi_ygivenx * pshi_x * all_weight_shi_energy[shi] # shape = B 
#                 print(Pshi_yx.shape,pshi_x.shape,pshi_ygivenx.shape,Pshi_energy.shape)
#                 raise
                
            # Score aggregation
            Psum = Px_mean + Pshi_energy
            Pmul = Px_mean * Pshi_energy
   
            cls_list.extend(cls[negative_target])
            if len(positive_target.shape) != 0:
                nd1.extend(Px_mean[positive_target].tolist())
                nd2.extend(Pygivenx_mean[positive_target].tolist())
                nd3.extend(Pshi_mean[positive_target].tolist())
                nd4.extend(Pshi_energy[positive_target].tolist())
                nd5.extend(Pshi_yx[positive_target].tolist())
                ndsum.extend(Psum[positive_target].tolist())
                ndmul.extend(Pmul[positive_target].tolist())
                
            if len(negative_target.shape) != 0:
                and1.extend(Px_mean[negative_target].tolist())
                and2.extend(Pygivenx_mean[negative_target].tolist())
                and3.extend(Pshi_mean[negative_target].tolist())
                and4.extend(Pshi_energy[negative_target].tolist())
                and5.extend(Pshi_yx[negative_target].tolist())
                andsum.extend(Psum[negative_target].tolist())
                andmul.extend(Pmul[negative_target].tolist())
    cal_class_auroc(nd1,nd2,nd3,nd4,nd5,and1,and2,and3,and4,and5,ndsum,andsum,ndmul,andmul,cls_list)
#     if valid_loader is not None:
#         print("calculating earlystop scores...")
#         escore = earlystop_score(model,valid_loader,all_weight_energy)
    
#     if return_minout:
#         m_in = -all_true_pen_energy_out_list.mean()
#         m_out = -all_false_pen_energy_out_list.mean()
#         print(m_in,m_out)
#         return m_in, m_out
#     return escore
    return
    
## 0) setting 
seed = args.seed
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.enabled = True
torch.manual_seed(seed)
np.random.seed(seed)
if torch.cuda.is_available():
    torch.cuda.manual_seed_all(seed)
np.random.seed(seed)

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
utils.makedirs(args.save_dir)
with open(f'{args.save_dir}/params.txt', 'w') as f: # training setting saving
    json.dump(args.__dict__, f)
if args.print_to_log: # 
    sys.stdout = open(f'{args.save_dir}/log.txt', 'w')

args.device = device
    
## 1) pretraining & prototyping
args.shift_trans, args.K_shift = C.get_shift_module()
args.shift_trans = args.shift_trans.to(device)

if args.dataset == 'cifar10':
    args.image_size = (32, 32, 3)
else:
    raise

model = C.get_classifier('resnet18', n_classes=10).to(device)
model = C.get_shift_classifer(model, 4).to(device)
simclr_aug = C.get_simclr_augmentation(args, image_size=args.image_size).to(device)

rotation = args.shift_trans 
criterion = nn.CrossEntropyLoss()


    
if args.load_path != None: # pretrained model loading
    ckpt_dict = torch.load(args.load_path)
    model.load_state_dict(ckpt_dict, strict = True)
else:
    assert False , "Not implemented error: you should give pretrained and prototyped model"
if torch.cuda.device_count() > 1:
    model = nn.DataParallel(model)
model.to(args.device)

# Transformation step followed by CSI (Rotation -> Augmentation -> Normalization)
train_transform = transforms.Compose([
    transforms.Resize((args.image_size[0], args.image_size[1])),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
])

test_transform = transforms.Compose([
    transforms.Resize((args.image_size[0], args.image_size[1])),
    transforms.ToTensor(),
])


# dataset loader
strong_aug = RandAugmentMC(n=12,m=5)
total_dataset = load_dataset("./data", normal_class=[args.known_normal], known_outlier_class=args.known_outlier,
                             n_known_outlier_classes=args.n_known_outlier, ratio_known_normal=args.ratio_known_normal,
                             ratio_known_outlier=args.ratio_known_outlier, ratio_pollution=args.ratio_pollution, random_state=None,
                             train_transform=train_transform, test_transform=test_transform,
                            valid_transform=strong_aug)


train_loader, false_valid_loader ,valid_loader, test_loader = total_dataset.loaders(batch_size = args.batch_size)
# Set prototypes (naive)

print("kmeans ",args.n_cluster)
n_cluster = args.n_cluster
protos = generate_prototypes(model, false_valid_loader, n_cluster=n_cluster, split=False)
model.module.prototypes = protos
model.module.prototypes = model.module.prototypes.to(args.device)

# model.module.prototypes = torch.rand(args.n_cluster, 128) - 0.5
# model.module.prototypes = F.normalize(model.module.prototypes, dim = -1)
# model.module.prototypes = model.module.prototypes.to(args.device)

params = model.parameters()
if args.optimizer == 'sgd':
    optim = optim.SGD(model.parameters(), lr=args.lr, momentum=0.9, weight_decay=args.weight_decay)
    lr_decay_gamma = 0.1
elif args.optimizer == 'adam':
    optim = optim.Adam(model.parameters(), lr=args.lr, betas=(.9, .999), weight_decay=args.weight_decay)
    lr_decay_gamma = 0.3
elif args.optimizer == 'lars':
    from torchlars import LARS
    base_optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=0.9, weight_decay=args.weight_decay)
    optim = LARS(base_optimizer, eps=1e-8, trust_coef=0.001)
    lr_decay_gamma = 0.1
elif args.optimizer == 'ranger':
    from ranger import Ranger
    optim = Ranger(model.parameters(), weight_decay=args.weight_decay,lr=args.lr)
else:
    raise NotImplementedError()
    
# normal_class=[args.known_normal], known_outlier_class=args.known_outlier,
print("known_normal:",args.known_normal,"known_outlier:",args.known_outlier)
# if args.lr_scheduler == 'cosine':
# scheduler = lr_scheduler.CosineAnnealingLR(optim, args.n_epochs)
# print("use cosine scheduler")
# Evaluation before training
rotation = args.shift_trans 
# m_in,m_out = test(model, test_loader, train_loader, -1,return_minout=True)
# escore = test(model, test_loader, train_loader, -1,valid_loader=valid_loader)
criterion = nn.CrossEntropyLoss()

earlystop_trace = []
end_train = False
max_earlystop_auroc = 0
for epoch in range(args.n_epochs):
    model.train()  
    for m in model.modules():
        if isinstance(m, nn.BatchNorm2d):
            m.eval()
#     # adjust learning rate 
    ## don't use decay 
#     print("we do not use lr decay")
#     if epoch in args.decay_epochs:
#         for param_group in optim.param_groups:
#             new_lr = param_group['lr'] * args.decay_rate
#             param_group['lr'] = new_lr
#         print("Decaying lr to {}".format(new_lr))
    # training
    losses_energy = []
    losses_shift = []
    for i, (pos, _, _, semi_target, _,_) in tqdm(enumerate(train_loader)):
        pos = pos.to(device)
        semi_target = semi_target.to(device)
        batch_size = pos.size(0)
        pos_1, pos_2 = hflip(pos.repeat(2, 1, 1, 1)).chunk(2)  # hflip              
        
        images1 = torch.cat([rotation(pos_1, k) for k in range(4)])
        images2 = torch.cat([rotation(pos_2, k) for k in range(4)])
        all_semi_target = semi_target.repeat(8)
        
        
        non_negative_target = (all_semi_target != -1)

        shift_labels = torch.cat([torch.ones_like(semi_target) * k for k in range(4)], 0).to(device)  # B -> 4B
        shift_labels = shift_labels.repeat(2)

        images_pair = torch.cat([images1, images2], dim=0)  # 8B
        images_pair = simclr_aug(images_pair)
        
        _, outputs_aux = model(images_pair, simclr=True, penultimate=True, shift=True)
        out = F.normalize(outputs_aux['simclr'],dim=-1)
        pen_out = outputs_aux['shift']
        
        Ls = criterion(pen_out[non_negative_target, :], shift_labels[non_negative_target])
        
        score = energy_score(out, model)
        C = (torch.log(torch.Tensor([args.n_cluster])) + 1/args.temperature).to(device)
        Le = torch.where(all_semi_target == -1, (C - score) ** -1, score ** -1).mean()  
        L = Le + Ls #+ Le_shi
        optim.zero_grad()
        L.backward()
        optim.step()
        
        ## optimizer scheduler
        
#         scheduler.step(epoch + i / len(train_loader))        
        
        losses_energy.append(Le.cpu().detach())
        losses_shift.append(Ls.cpu().detach())

    # earlystop
    model.eval()
    with torch.no_grad():
        earlystop_auroc = earlystop_score(model,valid_loader)
#         earlystop_loss = 0
#         for i, (pos, pos2, _, semi_target, _,_) in tqdm(enumerate(valid_loader)):
# #             pos = pos.to(device)
#             semi_target = semi_target.to(device)
#             batch_size = pos.size(0)
#             images1 = torch.cat([rotation(pos, k) for k in range(4)])
#             images2 = torch.cat([rotation(pos2, k) for k in range(4)])
#             images1 = images1.to(device)
#             images2 = images2.to(device)
#             images_pair = torch.cat([images1, images2], dim=0)  # 8B
#             images_pair = simclr_aug(images_pair)

#             all_semi_target = torch.cat([semi_target.repeat(4),(semi_target-1).repeat(4)])
# #             print(all_semi_target.shape)
#             non_negative_target = (all_semi_target != -1)

#             shift_labels = torch.cat([torch.ones_like(semi_target) * k for k in range(4)], 0).to(device)  # B -> 4B
#             shift_labels = shift_labels.repeat(2)


#             _, outputs_aux = model(images_pair, simclr=True, penultimate=True, shift=True)
#             out = F.normalize(outputs_aux['simclr'],dim=-1)
#             pen_out = outputs_aux['shift']
#             shift_labels = torch.cat([torch.ones_like(semi_target) * k for k in range(4)], 0).to(device)  # B -> 4B
#             shift_labels = shift_labels.repeat(2)
            
#             Ls = criterion(pen_out[non_negative_target, :], shift_labels[non_negative_target])

#             score = energy_score(out, model)
#             C = (torch.log(torch.Tensor([args.n_cluster])) + 1/args.temperature).to(device)
#             Le = torch.where(all_semi_target == -1, (C - score) ** -1, score ** -1).mean()
#             L = Le + Ls #+ Le_shi
#             earlystop_loss += L
# #         earlystop_loss /= len(valid_loader.dataset)
#         earlystop_trace.append(earlystop_loss.cpu())
    earlystop_trace.append(earlystop_auroc)
    print('[{}]epoch loss:'.format(epoch), np.mean(losses_energy), np.mean(losses_shift))
    print('[{}]earlystop loss:'.format(epoch),earlystop_auroc)
    
    if epoch % args.ckpt_every == 0 or epoch == args.n_epochs - 1: 
        checkpoint(model,  f'ckpt_ssl_{epoch}.pt', args, args.device)

    if max_earlystop_auroc < earlystop_auroc:
        max_earlystop_auroc = earlystop_auroc
        best_epoch = epoch
        checkpoint(model,  f'ckpt_ssl_{epoch}.pt', args, args.device)
        best_model = copy.deepcopy(model)
    # check earlystop condition
    if epoch>50:
        if earlystop_trace[-4] < max_earlystop_auroc and earlystop_trace[-3] < max_earlystop_auroc and earlystop_trace[-2] < max_earlystop_auroc:
            end_train = True
    
    if end_train:
        checkpoint(model,  f'ckpt_ssl_{epoch}.pt', args, args.device)
        print("trainin ended")
        break

print("best epoch:",best_epoch,"best auroc:",max_earlystop_auroc)
test(best_model, test_loader, train_loader, epoch, valid_loader=valid_loader) # we do not test them
