import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
from torch.autograd import Variable
from torchvision import models
import numpy as np
import copy


class DenseLayer(nn.Module):

    def __init__(self, n_channels, growth_rate=16):
        '''
        DenseNet Layer, as described in Jegou 2017.
        Returns concatenation of input and output along feature map
        dimension `1`.

        BN -> ReLU -> 3x3 Conv -> 2D Dropout (p=0.2)

        Parameters
        ----------
        n_channels : int. number of input channels.
        growth_rate : int. growth rate `k`, number of feature maps to add
            to the input before concatenation and output.
        '''

        super(DenseLayer, self).__init__()

        self.n_channels = n_channels
        self.growth_rate = growth_rate

        self.bn = nn.BatchNorm2d(self.n_channels)
        self.conv = nn.Conv2d(self.n_channels, self.growth_rate,
                kernel_size=3, padding=1, bias=False)
        self.do = nn.Dropout2d(p=0.2)

    def forward(self, x):
        out0 = F.relu(self.bn(x))
        out1 = self.conv(out0)
        out2 = self.do(out1)
        concat = torch.cat([x, out2], 1)
        return concat

class TransitionDown(nn.Module):

    def __init__(self, n_channels_in, n_channels_out=None):
        '''
        FC-DenseNet Transition Down module, as described in Jegou 2017.
        Returns downsampled image, preserving the number of feature maps by
        default.

        BN -> ReLU -> 1x1 Conv -> 2D Dropout (p=0.2) -> Max Pooling

        Parameters
        ----------
        n_channels_in : int. number of input channels
        n_channels_out : int, optional. number of output channels.
            preserves input by default.
        '''
        super(TransitionDown, self).__init__()

        self.n_channels_in = n_channels_in
        if n_channels_out is not None:
            self.n_channels_out = n_channels_out
        else:
            self.n_channels_out = self.n_channels_in

        self.bn = nn.BatchNorm2d(self.n_channels_in)
        self.conv = nn.Conv2d(self.n_channels_in, self.n_channels_out,
                            kernel_size=1, padding=0)
        self.pool = nn.MaxPool2d((2,2), stride=2)
        self.do = nn.Dropout2d(p=0.2)

    def forward(self, x):

        out0 = F.relu(self.bn(x))
        out1 = self.conv(out0)
        out2 = self.do(out1)
        pooled = self.pool(out2)
        return pooled

class TransitionUp(nn.Module):

    def __init__(self, n_channels_in, n_channels_out=None):
        '''
        FC-DenseNet Transition Up module, as described in Jegou 2017.
        Returns upsampled image by transposed convolution.

        3 x 3 Transposed Conv stride = 2

        Parameters
        ----------
        n_channels_in : int. number of input channels
        n_channels_out : int, optional. number of output channels.
            preserves input by default.
        '''

        super(TransitionUp, self).__init__()

        self.n_channels_in = n_channels_in
        if n_channels_out is not None:
            self.n_channels_out = n_channels_out
        else:
            self.n_channels_out = self.n_channels_in

        self.transconv = nn.ConvTranspose2d(
                self.n_channels_in,
                self.n_channels_out,
                kernel_size=3, stride=2)

    def forward(self, x):
        upsamp = self.transconv(x)
        return upsamp

class DenseBlock(nn.Module):

    def __init__(self, n_layers, n_channels, growth_rate=16, keep_input=True):
        '''
        Builds a DenseBlock from DenseLayers.
        As described in Jegou 2017.

        Parameters
        ----------
        n_layers : int. number of DenseLayers in the block.
        n_channels : int. number of input channels.
        growth_rate : int. growth rate `k`, number of feature maps to add
            to the input before concatenation and output.
        keep_input : boolean. concatenate the input to the newly added
            feature maps from this DenseBlock. input concatenation is omitted
            for DenseBlocks in the upsampling path of FC-DenseNet103.
        '''

        super(DenseBlock, self).__init__()

        self.n_layers = n_layers
        self.n_channels = n_channels
        self.growth_rate = growth_rate

        if keep_input:
            self.n_channels_out = n_channels + self.growth_rate*self.n_layers
        else:
            self.n_channels_out = self.growth_rate*self.n_layers

        self.block = self._build_block()

    def forward(self, x):
        out = self.block(x)
        if not keep_input:
            out = out[:,n_channels:,...] # omit input feature maps
        return out

    def _build_block(self):

        n_channels = self.n_channels
        layers = []

        for i in range(self.n_layers):
            l = DenseLayer(n_channels, self.growth_rate)
            layers.append(l)
            n_channels += self.growth_rate

        stack = nn.Sequential(*layers)

        return stack

class DenseNet103(nn.Module):

    def __init__(self, growth_rate=16, n_pool=4, n_classes=2,
                n_channels_first=48,
                n_layers_down=[4,5,7,10,12],
                n_layers_up=[12,10,7,5,4]):
        '''
        DenseNet 103 for semantic segmentation,
        as described in Jegou 2017.

        Parameters
        ----------
        growth_rate : int. growth rate `k`, number of feature maps to add
            to the input before concatenation and output.
        n_pool : int. number of pooling layers to incorporate
            in the downsampling and upsampling paths.
        n_classes : int. number of classes.
        n_channels_first : int. number of channels in the first 3x3 Conv layer.
        n_layers_down : array-like of int. number of layers in downsampling
            DenseBlocks. len == n_pool.
        n_layers_up : array-like of int. number of layers in upsampling
            DenseBlocks. len == n_pool.
        '''

        super(DenseNet103, self).__init__()

        self.growth_rate = growth_rate
        self.n_pool = n_pool
        self.n_classes = n_classes
        self.n_channels_first = n_channels_first
        self.n_layers_down = n_layers_down
        self.n_layers_up = n_layers_up

        if len(n_layers_down) != n_pool:
            raise ValueError('`n_layers_down` must be length `n_pool`')
        elif len(n_layers_up) == n_pool:
            raise ValueError('`n_layers_up` must be length `n_pool`')
        else:
            pass

        self.conv0 = nn.Conv2d(self.n_channels, self.n_channels_first,
                        kernel_size=3, stride=1, padding=1)

        # Downsampling path
        down_channels = self.n_channels_first
        for i in range(n_pool):
            setattr(self, 'down_dblock' + str(i),
                    DenseBlock(n_layers=self.n_layers_down[i],
                        n_channels=down_channels,
                        growth_rate=self.growth_rate))
            down_channels = getattr(self, 'down_dblock' + str(i)).n_channels_out
            setattr(self, 'td' + str(i),
                TransitionDown(n_channels_in=down_channels))

        # Bottleneck
        self.bottleneck = DenseBlock(n_layers=15,
                                n_channels=self.dblock4.n_channels_out,
                                growth_rate=self.growth_rate)

        # Upsampling path
        up_channels = self.bottleneck.n_channels_out
        for i in range(n_pool):
            setattr(self, 'tu' + str(i),
                    TransitionUp(n_channels_in=up_channels))
            setattr(self, 'up_dblock' + str(i),
                    DenseBlock(n_layers=self.n_layers_up[i],
                                n_channels=up_channels,
                                growth_rate=self.growth_rate,
                                keep_input=False))
            up_channels = getattr(self, 'up_dblock' + str(i)).n_channels_out

        self.conv1 = nn.Conv2d(
            getattr(self, 'up_dblock' + str(self.n_pool)).n_channels_out,
            self.n_classes,
            kernel_size=1)

    def forward(self, x):

        in_conv = self.conv0(x)

        out = in_conv

        # Downsampling path
        dblock_outs = []
        for i in range(self.n_pool):

            dblock = getattr('down_dblock' + str(i))
            td = getattr('td' + str(i))

            db_x = dblock(out)
            dblock_outs.append(db_x)

            out = td(db_x)

        # Bottleneck
        bneck = self.bottleneck(out)

        # Upsampling path
        out = bneck
        for i in range(self.n_pool):

            tu = getattr('tu' + str(i))
            ublock = getattr('up_dblock' + str(i))
            skip = dblock_outs[-i]

            up = tu(out)
            cat = torch.cat([skip, up])
            out = ublock(cat)

        classif = self.conv1(out)