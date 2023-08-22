# --------------------------------------------------------------------------
# Khai Nguyen | xkhai@cmu.edu
# Robotic Systems Lab, ETH Zürich
# Robotic Exploration Lab, Carnegie Mellon University
# See LICENSE file for the license information
# --------------------------------------------------------------------------
import torch
import torch.nn as nn
import torch.optim as optim

import operator
from functools import reduce
from torch.utils.data import TensorDataset, DataLoader
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

import numpy as np
import pickle
import time
from datetime import datetime
import os
import subprocess
import argparse
import sys
from os.path import normpath, dirname, join

sys.path.insert(0, normpath(join(dirname(__file__), "../..")))

from rayen import constraints, constraint_module, utils
from examples.early_stopping import EarlyStopping

# pickle is lazy and does not serialize class definitions or function
# definitions. Instead it saves a reference of how to find the class
# (the module it lives in and its name)
from CbfQpProblem import CbfQpProblem

# DEVICE = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
DEVICE = torch.device("cpu")
# torch.set_default_device(DEVICE)
torch.set_default_dtype(torch.float64)
np.set_printoptions(precision=4)

# generate axes object
ax = plt.axes()


# set limits
plt.xlim(-1, 1)
plt.ylim(-1, 1)
plt.xlabel("Position")
plt.ylabel("Velocity")


def main():
    utils.printInBoldBlue("CBF-QP Problem")
    print(f"{DEVICE = }")
    # Define problem
    args = {
        "prob_type": "cbf_qp",
        "xo": 1,
        "xc": 2,
        "nsamples": 18368,
        "method": "RAYEN",
        "hidden_size": 600,
    }
    print(args)

    # Load data, and put on GPU if needed
    prob_type = args["prob_type"]
    if prob_type == "cbf_qp":
        filepath = "data/cbf_qp_dataset_xo{}_xc{}_ex{}".format(
            args["xo"], args["xc"], args["nsamples"]
        )
    else:
        raise NotImplementedError

    with open(filepath, "rb") as f:
        data = pickle.load(f)

    for attr in dir(data):
        var = getattr(data, attr)
        if not callable(var) and not attr.startswith("__") and torch.is_tensor(var):
            try:
                setattr(data, attr, var.to(DEVICE))
            except AttributeError:
                pass

    data._device = DEVICE
    dir_dict = {}

    utils.printInBoldBlue("START INFERENCE")
    dir_dict["infer_dir"] = os.path.join(
        "results", str(data), "Aug16_09-31-47", "cbf_qp_net.dict"
    )

    model = CbfQpNet(data, args)
    model.load_state_dict(torch.load(dir_dict["infer_dir"]))
    model.eval()

    x0 = torch.Tensor([[[0.8]]])  # shape = (1, n, 1)
    v0 = torch.Tensor([[[0.3]]])  # shape = (1, n, 1)

    system = DoubleIntegrator(x0, v0, 1e-2)
    system_n = DoubleIntegrator(x0, v0, 1e-2)
    u_filtered = None
    un_filtered = None
    with torch.no_grad():
        for i in range(150):
            x, v = system.dynamics(u_filtered)
            xn, vn = system_n.dynamics(un_filtered)
            print(f"{x = }; {v = }")
            print(f"{xn = }; {vn = }")
            # u_nom = torch.distributions.uniform.Uniform(-1, 1.0).sample(
            #     [1, args["xo"], 1]
            # )  # (1, n, 1)
            u_nom = 1.5 * torch.tensor([[[np.cos(i / 3)]]])
            # u_nom = torch.tensor([[[2.0]]])
            un_filtered = nn_infer(model, xn, vn, u_nom)
            u_filtered = opt_solve(x, v, u_nom)

            print(f"{u_nom = }; {u_filtered = }")
            print(f"{u_nom = }; {un_filtered = } \n")

            # add something to axes
            ax.scatter(x, v, s=100.0, c="blue")
            ax.scatter(xn, vn, s=100.0, c="red", alpha=0.5)

            # draw the plot
            plt.draw()
            plt.pause(0.2)  # is necessary for the plot to update for some reason

            # start removing points if you don't want all shown
            if i > 0:
                ax.collections[0].remove()
                plt.legend(["nn", "opt"])
                ax.collections[1].remove()


def nn_infer(model, xn, vn, u_nom):
    input_n = torch.cat((u_nom, xn, vn), dim=1)
    un_filtered = model(input_n)
    un_filtered.nelement() == 0 and utils.printInBoldRed("NN failed")
    return un_filtered


def opt_solve(x, v, u_nom):
    xc = torch.cat([u_nom, x, v], 1).squeeze(-1)
    problem = CbfQpProblem(xc, 1, 2, 1)
    problem.updateObjective()
    problem.updateConstraints()
    problem.computeY()
    u_filtered = problem.Y
    u_filtered.nelement() == 0 and utils.printInBoldRed("Solver failed")
    return u_filtered


###################################################################
# SYSTEM
###################################################################
class DoubleIntegrator:
    def __init__(self, x0, v0, dt):
        self._x = x0
        self._v = v0
        self._dt = dt
        self._t = 0.0

    @property
    def x(self):
        return self._x

    @property
    def v(self):
        return self._v

    @property
    def dt(self):
        return self._dt

    @property
    def t(self):
        return self._t

    def dynamics(self, u=None):
        if u is not None:
            self._v += u * self.dt
        self._x += self._v * self.dt
        self._t += self.dt
        return self.x, self.v


###################################################################
# MODEL
###################################################################
class CbfQpNet(nn.Module):
    def __init__(self, data, args):
        super().__init__()
        self._data = data
        self._args = args

        # number of hidden layers and its size
        layer_sizes = [
            self._data.x_dim,
            self._args["hidden_size"],
            self._args["hidden_size"],
            self._args["hidden_size"],
        ]
        # layers = reduce(
        #     operator.add,
        #     [
        #         # [nn.Linear(a, b), nn.BatchNorm1d(b), nn.ReLU()]
        #         [nn.Linear(a, b), nn.BatchNorm1d(b), nn.ReLU(), nn.Dropout(p=0.1)]
        #         for a, b in zip(layer_sizes[0:-1], layer_sizes[1:])
        #     ],
        # )

        layers = [
            nn.Linear(layer_sizes[0], layer_sizes[1]),
            nn.ReLU(),
            nn.BatchNorm1d(layer_sizes[1]),
            nn.Linear(layer_sizes[1], layer_sizes[2]),
            nn.ReLU(),
            nn.Linear(layer_sizes[1], layer_sizes[2]),
        ]

        for layer in layers:
            if type(layer) == nn.Linear:
                nn.init.kaiming_normal_(layer.weight)

        self.nn_layer = nn.Sequential(*layers)

        self.rayen_layer = constraint_module.ConstraintModule(
            layer_sizes[-1],
            self._data.xc_dim,
            self._data.y_dim,
            self._args["method"],
            self._data.num_cstr,
            self._data.cstrInputMap,
        )

    def forward(self, x):
        x = x.squeeze(-1)
        xv = self.nn_layer(x)
        xc = x[:, self._data.xo_dim : self._data.xo_dim + self._data.xc_dim]
        y = self.rayen_layer(xv, xc)
        return y


if __name__ == "__main__":
    main()
    print()
