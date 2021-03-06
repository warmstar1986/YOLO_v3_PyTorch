from __future__ import division

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
from util import *

# 输入
def get_test_input():

  img = cv2.imread("dog-cycle-car.png")
  img = cv2.resize(img, (416,416)) # 调整输入图像维度
  img_ = img[:,:,::-1].transpose((2,0,1)) # BGR -> RGB | H x W x C -> C x H x W
  img_ = img_[np.newaxis,:,:,:]/255.0 # 添加通道，置0，作为正则通道
  img_ = torch.from_numpy(img_).float() # 转换成float
  img_ = Variable(img_) # 转成变量

  return img_

#Takes a cfg file,returns a list of blocks. 
def parse_cfg(cfgfile):

  file = open(cfgfile, 'r')
  lines = file.read().split('\n') # store lines in a list
  lines = [x for x in lines if len(x)>0] # get rid of empty lines
  lines = [x for x in lines if x[0] != '#'] # get rid of comments
  lines = [x.rstrip().lstrip() for x in lines] # get rid of whitespaces
  
  block = {}
  blocks = []

  for line in lines:
    if line[0] == "[": # a new block
      if len(block) != 0: # not empty
        blocks.append(block) # add blocks list
        block = {} # init blocks
      block["type"] = line[1:-1].rstrip()
    else:
      key, value = line.split("=")
      block[key.rstrip()] = value.lstrip()
  blocks.append(block)

  return blocks

# 空层
class EmptyLayer(nn.Module):
  def __init__(self):
    super(EmptyLayer, self).__init__() # 调用父类方法

# 定义一个新的DetectionLayer保存检测边界框的锚点
class DetectionLayer(nn.Module):
  def __init__(self, anchors):
    super(DetectionLayer, self).__init__()
    self.anchors = anchors

def create_modules(blocks):

  net_info = blocks[0] # input and pre-processing
  module_list = nn.ModuleList()
  prev_filters = 3 # depth of last conv
  output_filters = [] # number of output conv kernel，输出通道数量序列
  
  for index, x in enumerate(blocks[1:]):
    module = nn.Sequential()
    # convolutional模块有卷积层、批量归一化层和leaky ReLU激活层
    if (x["type"] == "convolutional"):
      # get layer info
      activation = x["activation"]
      try:
        batch_normalize = int(x["batch_normalize"])
        bias = False
      except:
        batch_normalize = 0
        bias = True

      filters = int(x["filters"]) # 卷积数量
      padding = int(x["pad"]) # 填充数量
      kernel_size = int(x["size"]) # 卷积核大小
      stride= int(x["stride"]) # 步长

      if padding:
        padding = (kernel_size - 1) // 2 # 运算后，宽度和高度不变
      else:
        padding = 0
      
      # Add conv layer
      conv = nn.Conv2d(prev_filters, filters, kernel_size, stride, padding, bias = bias)
      module.add_module("conv_{0}".format(index), conv)

      # Add batch norm layer
      if batch_normalize:
        bn = nn.BatchNorm2d(filters)
        module.add_module("batch_norm_{0}".format(index), bn)
      
      # Check activation
      if activation == "leaky":
        activn = nn.LeakyReLU(0.1, inplace = True) # 斜率0.1
        module.add_module("leaky_{0}".format(index), activn)

    # upsample上采样层
    elif (x["type"] == "upsample"):
      stride = int(x["stride"])
      upsample = nn.Upsample(scale_factor = 2, mode = "nearest") # 或者mode="bilinear"
      module.add_module("upsample_module_list{}".format(index), upsample)

    # route路由层，路由层是获取之前层的拼接
    elif (x["type"] == "route"):
      x["layers"] = x["layers"].split(",") # 保存start和end层号
      # Start of a route
      start = int(x["layers"][0])
      # end, if there exists one
      try:
        end = int(x["layers"][1])
      except:
        end = 0 # 没有end
      # Positive anotation
      if start > 0:
        start = start - index
      if end > 0:
        end = end - index
      route = EmptyLayer() # 创建空层
      module.add_module("route_{0}".format(index), route)
      if end < 0:
        # 计算卷积数量，即两层叠加
        filters = output_filters[index + start] + output_filters[index + end]
      else:
        filters = output_filters[index + start] 

    # shortcut捷径层（跳过连接），捷径层是将前一层的特征图添加到后面的层上
    elif (x["type"] == "shortcut"):
      shortcut = EmptyLayer()
      module.add_module("shortcut_{}".format(index), shortcut)

    # yolo层，检测层
    elif (x["type"] == "yolo"):
      # 保存mask序号
      mask = x["mask"].split(",")
      mask = [int(x) for x in mask]

      # 保存anchors box
      anchors = x["anchors"].split(",")
      anchors = [int(a) for a in anchors]
      # 两个一组，还ge和宽
      anchors = [(anchors[i], anchors[i+1]) for i in range(0, len(anchors), 2)]
      # 选取mask序号对应的anchors box，一般为3个
      anchors = [anchors[i] for i in mask]
      
      detection = DetectionLayer(anchors)
      module.add_module("Detection_{}".format(index), detection)
    
    module_list.append(module)
    prev_filters = filters
    output_filters.append(filters)

  return (net_info, module_list)

# 测试解析YOLO_v3配置文件
# blocks = parse_cfg("cfg/yolov3.cfg")
# print(create_modules(blocks))

class Darknet(nn.Module):
  # 用net_info和module_list对网络进行初始化
  def __init__(self, cfgfile):
    super(Darknet, self).__init__()
    self.blocks = parse_cfg(cfgfile)
    self.net_info, self.module_list = create_modules(self.blocks)

  # CUDA为true，则用GPU加速前向传播
  def forward(self, x, CUDA):

    # delf.blocks第一个元素是net块
    modules = self.blocks[1:]
    # 缓存每个层的输出特征图，以备route层和shortcut层使用
    outputs = {}

    write = 0 # 是否遇到第一个检测图flag
    for i, module in enumerate(modules):
      module_type = (module["type"])

      if module_type == "convolutional" or module_type == "upsample":
        x = self.module_list[i](x)

      elif module_type == "route":
        layers = module["layers"]
        layers = [int(a) for a in layers]

        if layers[0] > 0:
          layers[0] = layers[0] - i

        if len(layers) == 1:
          x = outputs[i + layers[0]]

        else:
          if layers[1] > 0:
            layers[1] = layers[1] - i

          map1 = outputs[i + layers[0]]
          map2 = outputs[i + layers[1]]
          x = torch.cat((map1, map2), 1) # 参数置1代表沿深度级联两个特征图

      elif module_type == "shortcut":
        from_ = int(module["from"])
        x = outputs[i-1] + outputs[i+from_]

      elif module_type == "yolo":
        anchors = self.module_list[i][0].anchors
        # input dimensions
        inp_dim = int(self.net_info["height"])
        
        # number of classes
        num_classes = int(module["classes"])

        # transform
        x = x.data
        x = predict_transform(x, inp_dim, anchors, num_classes, CUDA)

        if type(x) == int:
          continue
        # 如果收集器（容纳检测的张量）没有初始化
        if not write:
          detections = x
          write = 1
        else:
          detections = torch.cat((detections, x), 1)
      
      outputs[i] = x

    return detections

# 测试向前传播
# model = Darknet("cfg/yolov3.cfg")
# inp = get_test_input()
# pred = model(inp)
# print(pred)
# 张量形状1x10647x85，第一个维度是批量大小；85行，包括4个边界框属性(bx,by,bh,bw)、1个objectness分数和80个类别分数

  def load_weights(self, weightfile):

    """
    权重属于归一化层和卷积层，权重存储顺序与配置文件层级顺序一致。
    conv有shortcut，shortcut连接另一个conv，则先包含先前conv权重。
    conv with batch norm：bn biases,bn weights,bn running_mean,bn running_var,conv weights
    conv no batch norm：conv biases,conv weights
    """
    fp = open(weightfile, "rb")

    # 标题信息：主版本，次版本，子版本，训练期间网络看到的图像
    header = np.fromfile(fp, dtype = np.int32, count = 5)
    self.header = torch.from_numpy(header)
    self.seen = self.header[3]
    
    weights = np.fromfile(fp, dtype = np.float32)
    # 迭代地加载权重文件到网络的模块上
    ptr = 0 # 追踪权重数组位置指针
    for i in range(len(self.module_list)):
      module_type = self.blocks[i + 1]["type"] # 块包含第一块，模块不包含第一块

      if module_type == "convolutional":
        model = self.module_list[i]
        # 根据conv模块是否有batch_normalize，加载权重
        try:
          batch_normalize = int(self.blocks[i+1]["batch_normalize"])
        except:
          batch_normalize = 0

        conv = model[0]

        # conv with batch norm
        if (batch_normalize):
          bn = model[1]
          # 获取b_n layer权重的数量
          num_bn_bias = bn.bias.numel()
          # 加载权重
          bn_bias = torch.from_numpy(weights[ptr:ptr+num_bn_bias])
          ptr += num_bn_bias

          bn_weight = torch.from_numpy(weights[ptr:ptr+num_bn_bias])
          ptr += num_bn_bias

          bn_running_mean = torch.from_numpy(weights[ptr:ptr+num_bn_bias])
          ptr += num_bn_bias

          bn_running_var = torch.from_numpy(weights[ptr:ptr+num_bn_bias])
          ptr += num_bn_bias
          # 根据模型权重的维度调整重塑加载的权重
          bn_bias = bn_bias.view_as(bn.bias.data)
          bn_weight = bn_weight.view_as(bn.weight.data)
          bn_running_mean = bn_running_mean.view_as(bn.running_mean)
          bn_running_var = bn_running_var.view_as(bn.running_var)
          # 将数据复制到模型中
          bn.bias.data.copy_(bn_bias)
          bn.weight.data.copy_(bn_weight)
          bn.running_mean.copy_(bn_running_mean)
          bn.running_var.copy_(bn_running_var)

        # conv no batch norm，只加载卷积层的偏置项
        else:
          # 偏置数量
          num_bias = conv.bias.numel()
          # 加载权重
          conv_bias = torch.from_numpy(weights[ptr:ptr+num_bias])
          ptr += num_bias
          # 根据模型权重的维度调整重塑加载的权重
          conv_bias = conv_bias.view_as(conv.bias.data)
          # 将数据复制到模型中
          conv.bias.data.copy_(conv_bias)
        
        # 最后，加载共有的卷积层权重
        num_weight  = conv.weight.numel()
        conv_weight = torch.from_numpy(weights[ptr:ptr+num_weight])
        ptr += num_weight
        conv_weight = conv_weight.view_as(conv.weight.data)
        conv.weight.data.copy_(conv_weight)
    
# 测试加载预训练权重
# model = Darknet("cfg/yolov3.cfg")
# model.load_weights("yolov3.weights")
# inp = get_test_input()
# pred = model(inp)
# print(pred)






  