from ptsemseg.models.utils import *
from torchvision import models
import torchvision
from torch.nn import functional as F


def conv3x3(in_, out):
    return nn.Conv2d(in_, out, 3, padding=1)


def encoder_rgbs(m, n_classes):
    [out_, in_] = m.weight.shape[0:2]
    mean_c = torch.mean(m.weight, 1, True)
    list_c = [m.weight] + [mean_c] * n_classes

    conv_rgbs = conv3x3(in_ + n_classes, out_)
    conv_rgbs.weight = torch.nn.Parameter(torch.cat(list_c, 1))
    conv_rgbs.bias = m.bias
    return conv_rgbs


class ConvRelu(nn.Module):
    def __init__(self, in_: int, out: int):
        super(ConvRelu, self).__init__()
        self.conv = conv3x3(in_, out)
        self.activation = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.activation(x)
        return x


class ConvBlock(nn.Module):
    def __init__(self, in_channels, middle_channels, out_channels):
        super(ConvBlock, self).__init__()

        self.block = nn.Sequential(
            ConvRelu(in_channels, middle_channels),
            ConvRelu(middle_channels, out_channels),
        )

    def forward(self, x):
        return self.block(x)


class DecoderBlock(nn.Module):
    """
    Paramaters for Deconvolution were chosen to avoid artifacts, following
    link https://distill.pub/2016/deconv-checkerboard/
    """

    def __init__(self, in_channels, middle_channels, out_channels, is_deconv=True):
        super(DecoderBlock, self).__init__()
        self.in_channels = in_channels

        if is_deconv:
            self.block = nn.Sequential(
                ConvRelu(in_channels, middle_channels),
                nn.ConvTranspose2d(middle_channels, out_channels, kernel_size=4, stride=2,
                                   padding=1),
                nn.ReLU(inplace=True)
            )
        else:
            self.block = nn.Sequential(
                nn.Upsample(scale_factor=2, mode='bilinear'),
                ConvRelu(in_channels, middle_channels),
                ConvRelu(middle_channels, out_channels),
            )

    def forward(self, x):
        return self.block(x)


class druvgg16(nn.Module):
    def __init__(self,
                 args,
                 n_classes=19,
                 initial=1,
                 steps=3,
                 gate=3,
                 hidden_size=32*8,
                 feature_scale=1,
                 is_deconv=True,
                 in_channels=3,
                 is_batchnorm=True,
                 num_filters=32,
                 pretrained=True
                 ):
        super(druvgg16, self).__init__()
        self.args = args
        self.steps = steps
        self.feature_scale = feature_scale
        self.hidden_size = hidden_size
        self.in_channels = in_channels
        self.n_classes = n_classes
        self.is_batchnorm = is_batchnorm
        self.is_deconv = is_deconv
        self.num_filters = num_filters

        self.pool = nn.MaxPool2d(2, 2)

        self.encoder = torchvision.models.vgg16(pretrained=pretrained).features

        self.relu = nn.ReLU(inplace=True)

        self.encoder_rgb_s = encoder_rgbs(self.encoder[0], self.n_classes)

        self.conv1 = nn.Sequential(self.encoder_rgb_s,
                                   self.relu,
                                   self.encoder[2],
                                   self.relu)

        self.conv2 = nn.Sequential(self.encoder[5],
                                   self.relu,
                                   self.encoder[7],
                                   self.relu)

        self.conv3 = nn.Sequential(self.encoder[10],
                                   self.relu,
                                   self.encoder[12],
                                   self.relu,
                                   self.encoder[14],
                                   self.relu)

        self.conv4 = nn.Sequential(self.encoder[17],
                                   self.relu,
                                   self.encoder[19],
                                   self.relu,
                                   self.encoder[21],
                                   self.relu)

        self.conv5 = nn.Sequential(self.encoder[24],
                                   self.relu,
                                   self.encoder[26],
                                   self.relu,
                                   self.encoder[28],
                                   self.relu)

        self.gru = ConvDRU(512, self.hidden_size)
        assert (self.hidden_size == num_filters * 8), \
            f'hidden size {self.hidden_size} should be num_filter * 8 = {num_filters * 8 }'
        # self.center = DecoderBlock(512, num_filters * 8 * 2, num_filters * 8)
        self.dec5 = DecoderBlock(512 + num_filters * 8, num_filters * 8 * 2, num_filters * 8)
        self.dec4 = DecoderBlock(512 + num_filters * 8, num_filters * 8 * 2, num_filters * 8)
        self.dec3 = DecoderBlock(256 + num_filters * 8, num_filters * 4 * 2, num_filters * 2)
        self.dec2 = DecoderBlock(128 + num_filters * 2, num_filters * 2 * 2, num_filters)
        self.dec1 = ConvRelu(64 + num_filters, num_filters)
        self.final = nn.Conv2d(num_filters, n_classes, kernel_size=1)

        self.paramGroup1 = nn.Sequential(self.conv1, self.conv2, self.conv3, self.conv4, self.conv5)
        self.paramGroup2 = nn.Sequential(self.gru, self.dec5, self.dec4, self.dec3, self.dec2, self.dec1, self.final)

    def forward(self, inputs, h, s):
        list_st = []
        for i in range(self.steps):
            stacked_inputs = torch.cat([inputs, s], dim=1)

            conv1 = self.conv1(stacked_inputs)
            conv2 = self.conv2(self.pool(conv1))
            conv3 = self.conv3(self.pool(conv2))
            conv4 = self.conv4(self.pool(conv3))
            conv5 = self.conv5(self.pool(conv4))

            center = self.gru(self.pool(conv5), h)

            dec5 = self.dec5(torch.cat([center, conv5], 1))

            dec4 = self.dec4(torch.cat([dec5, conv4], 1))
            dec3 = self.dec3(torch.cat([dec4, conv3], 1))
            dec2 = self.dec2(torch.cat([dec3, conv2], 1))
            dec1 = self.dec1(torch.cat([dec2, conv1], 1))

            # if self.n_classes > 1:
            #     x_out = F.log_softmax(self.final(dec1), dim=1)
            # else:
            #     x_out = self.final(dec1)
            s = self.final(dec1)

            list_st += [s]

        return list_st


class ConvDRU(nn.Module):
    def __init__(self,
                 input_size,
                 hidden_size,
                 ):
        super(ConvDRU, self).__init__()

        self.reset_gate = ConvBlock(input_size, input_size, input_size)
        self.update_gate = DecoderBlock(input_size, hidden_size*2, hidden_size)
        self.out_gate = DecoderBlock(input_size, hidden_size*2, hidden_size)

    def forward(self, input_, h=None):
        # batch_size = input_.data.size()[0]
        # spatial_size = input_.data.size()[2:]

        # data size is [batch, channel, height, width]
        # print('input_.type', input_.data.type())
        # print('prev_state.type', prev_state.data.type())

        update = torch.sigmoid(self.update_gate(input_))
        reset = torch.sigmoid(self.reset_gate(input_))
        # print('input_, update, reset, h, shape ', input_.shape, update.shape, reset.shape, h.shape)
        # stacked_inputs_ = torch.cat([input_, h * reset], dim=1)
        # out_inputs = torch.tanh(self.out_gate(stacked_inputs_))
        out_inputs = torch.tanh(self.out_gate(input_ * reset))
        h_new = h * (1 - update) + out_inputs * update
        return h_new

    def __repr__(self):
        return 'ConvDRU: \n' + \
               '\t reset_gate: \n {}\n'.format(self.reset_gate.__repr__()) + \
               '\t update_gate: \n {}\n'.format(self.update_gate.__repr__()) + \
               '\t out_gate:  \n {}\n'.format(self.out_gate.__repr__())
