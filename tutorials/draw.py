import ipdb
import numpy as np
import theano
import theano.tensor as T

from cle.cle.data import Iterator
from cle.cle.cost import NllBin
from cle.cle.graph.net import Net
from cle.cle.models import Model
from cle.cle.models.draw import (
    CanvasLayer,
    ErrorLayer,
    ReadLayer,
    WriteLayer
)
from cle.cle.layers import InitCell, RealVectorLayer
from cle.cle.layers.conv import ConvertLayer
from cle.cle.layers.feedforward import FullyConnectedLayer
from cle.cle.layers.layer import PriorLayer
from cle.cle.layers.recurrent import LSTM
from cle.cle.train import Training
from cle.cle.train.ext import (
    EpochCount,
    GradientClipping,
    Monitoring,
    Picklize,
    EarlyStopping
)
from cle.cle.train.opt import Adam
from cle.cle.utils import flatten
from cle.cle.utils.compat import OrderedDict
from cle.datasets.mnist import MNIST


datapath = '/home/junyoung/data/mnist/mnist.pkl'
savepath = '/home/junyoung/repos/cle/saved/'

batch_size = 128
inpsz = 784
latsz = 50
debug = 1

model = Model()
data = MNIST(name='train',
             path=datapath)

init_W = InitCell('rand')
init_U = InitCell('ortho')
init_b = InitCell('zeros')

model.inputs = data.theano_vars()
x, _ = model.inputs
y = x.copy()
if debug:
    x.tag.test_value = np.zeros((batch_size, 784), dtype=np.float32)
    y.tag.test_value = np.zeros((batch_size, 784), dtype=np.float32)

inputs = [x]
inputs_dim = {'x':inpsz}
read = ReadLayer(name='read',
                 parent=['x', 'error'],
                 parent_dim=[784, 784],
                 recurrent=['dec'],
                 recurrent_dim=[500],
                 nout=288,
                 N=12,
                 img_shape=(batch_size, 1, 28, 28),
                 batch_size=batch_size,
                 init_U=InitCell('rand'))
enc = LSTM(name='enc',
           parent=['read'],
           parent_dim=[288],
           recurrent=['dec'],
           recurrent_dim=[500],
           batch_size=batch_size,
           nout=500,
           unit='tanh',
           init_W=init_W,
           init_U=init_U,
           init_b=init_b)
phi_mu = FullyConnectedLayer(name='phi_mu',
                             parent=['enc'],
                             parent_dim=[500],
                             nout=latsz,
                             unit='linear',
                             init_W=init_W,
                             init_b=init_b)
phi_var = RealVectorLayer(name='phi_var',
                          nout=latsz,
                          init_b=init_b)
prior = PriorLayer(name='prior',
                   parent=['phi_mu', 'phi_var'],
                   parent_dim=[latsz, latsz],
                   use_sample=1,
                   nout=latsz)
kl = PriorLayer(name='kl',
                parent=['phi_mu', 'phi_var'],
                   parent_dim=[latsz, latsz],
                use_sample=0,
                nout=latsz)
dec = LSTM(name='dec',
           parent=['prior'],
           parent_dim=[latsz],
           batch_size=batch_size,
           nout=500,
           unit='tanh',
           init_W=init_W,
           init_U=init_U,
           init_b=init_b)
w1 = FullyConnectedLayer(name='w1',
                         parent=['dec'],
                         parent_dim=[500],
                         nout=144,
                         unit='linear',
                         init_W=init_W,
                         init_b=init_b)
write= WriteLayer(name='write',
                  parent=['w1', 'dec'],
                  parent_dim=[144, 500],
                  nout=784,
                  N=28,
                  img_shape=(batch_size, 1, 12, 12))
error = ErrorLayer(name='error',
                   parent=['x'],
                   parent_dim=[784],
                   recurrent=['canvas'],
                   recurrent_dim=[784],
                   is_binary=1,
                   nout=inpsz,
                   batch_size=batch_size)
canvas = CanvasLayer(name='canvas',
                     parent=['write'],
                     parent_dim=[784],
                     nout=inpsz,
                     batch_size=batch_size)

nodes = [read, enc, phi_mu, phi_var, prior, kl, dec, w1, write, error, canvas]
for node in nodes:
    node.initialize()
params = flatten([node.get_params().values() for node in nodes])

def inner_fn(enc_h_tm1, dec_h_tm1, canvas_h_tm1, x, phi_var_out):
    err_out = error.fprop([[x], [canvas_h_tm1]])
    read_out = read.fprop([[x, err_out], [enc_h_tm1, dec_h_tm1]])
    enc_out = enc.fprop([[read_out], [enc_h_tm1, dec_h_tm1]])
    phi_mu_out = phi_mu.fprop([enc_out])
    prior_out = prior.fprop([phi_mu_out, phi_var_out])
    kl_out = kl.fprop([phi_mu_out, phi_var_out])
    dec_out = dec.fprop([[prior_out], [dec_h_tm1]])
    w1_out = w1.fprop([dec_out])
    write_out = write.fprop([w1_out, dec_out])
    canvas_out = canvas.fprop([[write_out], [canvas_h_tm1]])
    error_out = error.fprop([[x], [canvas_out]])

    return enc_out, dec_out, canvas_out, kl_out

phi_var_out = phi_var.fprop()
((enc_, dec_, canvas_, kl_), updates) = theano.scan(fn=inner_fn,
                                                    outputs_info=[enc.get_init_state(),
                                                                  dec.get_init_state(),
                                                                  canvas.get_init_state(),
                                                                  None],
                                                    non_sequences=[x, phi_var_out],
                                                    n_steps=20)
recon_term = NllBin(y, T.nnet.sigmoid(canvas_[-1])).sum()
kl_term = (kl_.sum(axis=2).sum(axis=0)).sum()
cost = recon_term + kl_term
cost.name = 'cost'
recon_term.name = 'recon_term'
kl_term.name = 'kl_term'
recon_err = ((y - T.nnet.sigmoid(canvas_[-1]))**2).mean() / y.std()
recon_err.name = 'recon_err'
model.inputs = [x, y]
model._params = params

optimizer = Adam(
    lr=0.001
)

extension = [
    GradientClipping(),
    EpochCount(10000),
    Monitoring(freq=50,
               ddout=[cost, recon_term, kl_term, recon_err],
               data=[Iterator(data, batch_size)]),
    Picklize(freq=200,
             path=savepath),
    EarlyStopping(path=savepath)
]

mainloop = Training(
    name='draw',
    data=Iterator(data, batch_size),
    model=model,
    optimizer=optimizer,
    cost=cost,
    outputs=[cost],
    extension=extension
)
mainloop.run()
