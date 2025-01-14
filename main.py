import os 
import random 
import time
import json
import torch
import torchvision
import numpy as np 
import pandas as pd 
import warnings
from datetime import datetime
from torch import nn,optim
from config import config 
from collections import OrderedDict
from torch.autograd import Variable 
from torch.utils.data import DataLoader
from dataset.dataloader import *
from sklearn.model_selection import train_test_split,StratifiedKFold
from timeit import default_timer as timer
from models.model import *
from utils import *

# 设置随机数
random.seed(config.seed)
np.random.seed(config.seed)
torch.manual_seed(config.seed)
torch.cuda.manual_seed_all(config.seed)
os.environ["CUDA_VISIBLE_DEVICES"] = config.gpus
torch.backends.cudnn.benchmark = True
warnings.filterwarnings('ignore')

# 评估函数
def evaluate(val_loader,model,criterion):
    # Meter类用来跟踪一些统计量的，
    # 能够在一段“历程”中记录下某个统计量在迭代过程中不断变化的值，并统计相关的量。
    #2.1 define meters
    losses = AverageMeter()
    top1 = AverageMeter()
    top2 = AverageMeter()

    # 调整到evaluate mode
    model.cuda()
    model.eval()
    with torch.no_grad():
        for i, (input,target) in enumerate(val_loader):
            input = Variable(input).cuda()
            target = Variable(torch.from_numpy(np.array(target)).long()).cuda()

            # 计算output
            output = model(input)
            loss = criterion(output,target)

            # 计算精度和损失
            precision1,precision2 = accuracy(output, target, topk=(1,2))
            losses.update(loss.item(),input.size(0))
            top1.update(precision1[0],input.size(0))
            top2.update(precision2[0],input.size(0))

    return [losses.avg,top1.avg,top2.avg]

# 测试函数
def test(test_loader, model,folds):
    csv_map = OrderedDict({"filename":[],"probability":[]})
    model.cuda()
    model.eval()
    with open("./submit/baseline.json","w",encoding="utf-8") as f :
        submit_results = []
        for i,(input,filepath) in enumerate(tqdm(test_loader)):

            #3.2 change everything to cuda and get only basename
            filepath = [os.path.basename(x) for x in filepath]
            with torch.no_grad():
                image_var = Variable(input).cuda()
                # output
                y_pred = model(image_var)

                smax = nn.Softmax(1)
                smax_out = smax(y_pred)
            #3.4 save probability to csv files
            csv_map["filename"].extend(filepath)
            for output in smax_out:
                prob = ";".join([str(i) for i in output.data.tolist()])
                csv_map["probability"].append(prob)
        result = pd.DataFrame(csv_map)
        result["probability"] = result["probability"].map(lambda x : [float(i) for i in x.split(";")])
        for index, row in result.iterrows():
            pred_label = np.argmax(row['probability'])
            if pred_label > 43:
                pred_label = pred_label + 2
            submit_results.append({"image_id":row['filename'],"disease_class":pred_label})
        json.dump(submit_results,f,ensure_ascii=False,cls = MyEncoder)

   
def main():
    fold = 0
    # 创建文件夹
    if not os.path.exists(config.submit):
        os.mkdir(config.submit)
    if not os.path.exists(config.weights):
        os.mkdir(config.weights)
    if not os.path.exists(config.best_models):
        os.mkdir(config.best_models)
    if not os.path.exists(config.logs):
        os.mkdir(config.logs)
    if not os.path.exists(config.weights + config.model_name + os.sep +str(fold) + os.sep):
        os.makedirs(config.weights + config.model_name + os.sep +str(fold) + os.sep)
    if not os.path.exists(config.best_models + config.model_name + os.sep +str(fold) + os.sep):
        os.makedirs(config.best_models + config.model_name + os.sep +str(fold) + os.sep)       

    # 获得模型和优化器
    model = get_net()
    #model = torch.nn.DataParallel(model)
    model.cuda()
    #optimizer = optim.SGD(model.parameters(),lr = config.lr,momentum=0.9,weight_decay=config.weight_decay)
    optimizer = optim.Adam(model.parameters(),lr = config.lr,amsgrad=True,weight_decay=config.weight_decay)
    criterion = nn.CrossEntropyLoss().cuda()  # 损失函数
    #criterion = FocalLoss().cuda()
    log = Logger()
    log.open(config.logs + "log_train.txt",mode="a")
    log.write("\n----------------------------------------------- [START %s] %s\n\n" % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), '-' * 51))
    
    # some parameters for  K-fold and restart model
    start_epoch = 0
    best_precision1 = 0
    best_precision_save = 0
    resume = False
    
    # restart the training process
    if resume:
        checkpoint = torch.load(config.best_models + str(fold) + "/model_best.pth.tar")
        start_epoch = checkpoint["epoch"]
        fold = checkpoint["fold"]
        best_precision1 = checkpoint["best_precision1"]
        model.load_state_dict(checkpoint["state_dict"])
        optimizer.load_state_dict(checkpoint["optimizer"])

    #4.5 get files and split for K-fold dataset
    #4.5.1 read files
    train_ = get_files(config.train_data,"train")
    #val_data_list = get_files(config.val_data,"val")
    test_files = get_files(config.test_data,"test")

    
    # 划分训练集和验证集
    train_data_list, val_data_list = train_test_split(train_, test_size = 0.15, stratify=train_["label"])
    # load dataset
    # pin_memory就是锁页内存
    # 当计算机的内存充足的时候，可以设置pin_memory=True。
    # 当系统卡住，或者交换内存使用过多的时候，设置pin_memory=False。
    train_dataloader = DataLoader(ChaojieDataset(train_data_list),batch_size=config.batch_size,shuffle=True,collate_fn=collate_fn,pin_memory=True)
    val_dataloader = DataLoader(ChaojieDataset(val_data_list,train=False),batch_size=config.batch_size,shuffle=True,collate_fn=collate_fn,pin_memory=False)
    test_dataloader = DataLoader(ChaojieDataset(test_files,test=True),batch_size=1,shuffle=False,pin_memory=False)
    #scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer,"max",verbose=1,patience=3)
    
    # 调整优化器（多少epoch更新一次lr, 以及更新倍数）
    scheduler =  optim.lr_scheduler.StepLR(optimizer,step_size = 10,gamma=0.1)
    # define metrics
    train_losses = AverageMeter()
    train_top1 = AverageMeter()
    train_top2 = AverageMeter()
    valid_loss = [np.inf,0,0]

    # model.train()和model.eval()两个函数通过改变self.training = True / False 来告知一些特定的层（BN,Dropout）
    # 应该启用 train 时的功能还是 test 时的功能。
    model.train()
    # logs
    log.write('** start training here! **\n')
    log.write('                           |------------ VALID -------------|----------- TRAIN -------------|------Accuracy------|------------|\n')
    log.write('lr       iter     epoch    | loss   top-1  top-2            | loss   top-1  top-2           |    Current Best    | time       |\n')
    log.write('-------------------------------------------------------------------------------------------------------------------------------\n')
    # train
    start = timer()
    for epoch in range(start_epoch,config.epochs):
        scheduler.step(epoch)  # 用于更新lr
        # train
        # global iter
        for iter,(input,target) in enumerate(train_dataloader):
            #4.5.5 switch to continue train process
            model.train()
            # pytorch两个基本对象：Tensor（张量）和Variable（变量）
            # tensor不能反向传播，variable可以反向传播。
            input = Variable(input).cuda()
            target = Variable(torch.from_numpy(np.array(target)).long()).cuda()
            #target = Variable(target).cuda()
            output = model(input)
            loss = criterion(output,target)

            precision1_train, precision2_train = accuracy(output, target, topk=(1,2))
            train_losses.update(loss.item(), input.size(0))
            train_top1.update(precision1_train[0],input.size(0))
            train_top2.update(precision2_train[0],input.size(0))
            #backward
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            lr = get_learning_rate(optimizer)
            print('\r',end='',flush=True)
            print('%0.4f %5.1f %6.1f        | %0.3f  %0.3f  %0.3f         | %0.3f  %0.3f  %0.3f         |         %s         | %s' % (\
                         lr, iter/len(train_dataloader) + epoch, epoch,
                         valid_loss[0], valid_loss[1], valid_loss[2],
                         train_losses.avg, train_top1.avg, train_top2.avg,str(best_precision_save),
                         time_to_str((timer() - start),'min'))
            , end='',flush=True)
        # evaluate
        lr = get_learning_rate(optimizer)
        # evaluate every half epoch
        valid_loss = evaluate(val_dataloader, model, criterion)
        is_best = valid_loss[1] > best_precision1
        best_precision1 = max(valid_loss[1], best_precision1)
        try:
            best_precision_save = best_precision1.cpu().data.numpy()
        except:
            pass
        save_checkpoint({
                    "epoch":epoch + 1,
                    "model_name":config.model_name,
                    "state_dict":model.state_dict(),
                    "best_precision1":best_precision1,
                    "optimizer":optimizer.state_dict(),
                    "fold":fold,
                    "valid_loss":valid_loss,
        },is_best, fold)
        # adjust learning rate
        # scheduler.step(valid_loss[1])
        print("\r",end="",flush=True)
        log.write('%0.4f %5.1f %6.1f        | %0.3f  %0.3f  %0.3f          | %0.3f  %0.3f  %0.3f         |         %s         | %s' % (\
                        lr, 0 + epoch, epoch,
                        valid_loss[0], valid_loss[1], valid_loss[2],
                        train_losses.avg,    train_top1.avg,    train_top2.avg, str(best_precision_save),
                        time_to_str((timer() - start),'min'))
                )
        log.write('\n')
        time.sleep(0.01)
    best_model = torch.load(config.best_models + os.sep+config.model_name+os.sep+ str(fold) +os.sep+ 'model_best.pth.tar')
    model.load_state_dict(best_model["state_dict"])
    test(test_dataloader,model,fold)

if __name__ =="__main__":
    main()





















