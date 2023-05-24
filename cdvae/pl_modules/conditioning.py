"""p(x|y)"""
import hydra
import torch
import torch.nn as nn
from torch_scatter import scatter

from cdvae.pl_modules.basic_blocks import build_mlp
from cdvae.pl_modules.embeddings import KHOT_EMBEDDINGS, MAX_ATOMIC_NUM
from cdvae.pl_modules.gemnet.layers.embedding_block import AtomEmbedding


class SubEmbedding(nn.Module):
    def __init__(self, n_out):
        super().__init__()
        self.n_out = n_out


class CompositionEmbedding(SubEmbedding):
    def __init__(self, n_out, reduce='mean'):
        super().__init__(n_out)
        self.n_out = n_out
        self.reduce = reduce
        self.emb = AtomEmbedding(n_out)

    def forward(self, prop):
        atom_types, num_atoms = prop
        batch = torch.repeat_interleave(
            torch.arange(num_atoms.size(0), device=num_atoms.device),
            num_atoms,
        )
        atom_emb = self.emb(atom_types)
        comp_emb = scatter(atom_emb, batch, dim=0, reduce=self.reduce)
        return comp_emb


# single scalar, one sample is (1,)
class ScalarEmbedding(SubEmbedding):
    def __init__(
        self,
        prop_name: str,
        # batch norm
        batch_norm: bool = False,
        # gaussian expansion
        no_expansion: bool = False,
        n_basis: int = None,  # num gaussian basis
        start: float = None,
        stop: float = None,
        trainable_gaussians: bool = False,
        width: float = None,
        # out mlp
        no_mlp: bool = False,
        hidden_dim: int = None,
        fc_num_layers: int = None,
        n_out: int = None,
    ):
        super().__init__(n_out)
        self.n_out = n_out
        self.prop_name = prop_name

        if batch_norm:
            self.bn = nn.BatchNorm1d(1)
        else:
            self.bn = nn.Identity()

        if no_expansion:
            self.expansion_net = nn.Identity()
        else:
            self.expansion_net = GaussianExpansion(
                start, stop, n_basis, trainable_gaussians, width
            )

        if no_mlp:
            self.mlp = nn.Identity()
        else:
            self.mlp = build_mlp(None, hidden_dim, fc_num_layers, n_out)

    def forward(self, prop):
        prop = self.bn(prop)
        prop = self.expansion_net(prop)  # expanded prop
        out = self.mlp(prop)
        return out


class VectorEmbedding(SubEmbedding):
    def __init__(self, prop_name, n_in, hidden_dim, fc_num_layers, n_out):
        super().__init__(n_out)
        self.prop_name = prop_name
        self.mlp = build_mlp(n_in, hidden_dim, fc_num_layers, n_out)

    def forward(self, batch):
        prop = batch[self.prop_name]
        return self.mlp(prop)


# ## [cG-SchNet](
# ## MISC
class GaussianExpansion(nn.Module):
    r"""Expansion layer using a set of Gaussian functions.

    https://github.com/atomistic-machine-learning/cG-SchNet/blob/53d73830f9fb1158296f060c2f82be375e2bb7f9/nn_classes.py#L687)

    Args:
        start (float): center of first Gaussian function, :math:`\mu_0`.
        stop (float): center of last Gaussian function, :math:`\mu_{N_g}`.
        n_gaussians (int, optional): total number of Gaussian functions, :math:`N_g`
            (default: 50).
        trainable (bool, optional): if True, widths and offset of Gaussian functions
            are adjusted during training process (default: False).
        widths (float, optional): width value of Gaussian functions (provide None to
            set the width to the distance between two centers :math:`\mu`, default:
            None).
    """

    def __init__(self, start, stop, n_gaussians=50, trainable=False, width=None):
        super(GaussianExpansion, self).__init__()
        # compute offset and width of Gaussian functions
        offset = torch.linspace(start, stop, n_gaussians)
        if width is None:
            widths = torch.FloatTensor(
                (offset[1] - offset[0]) * torch.ones_like(offset)
            )
        else:
            widths = torch.FloatTensor(width * torch.ones_like(offset))
        if trainable:
            self.widths = nn.Parameter(widths)
            self.offsets = nn.Parameter(offset)
        else:
            self.register_buffer("widths", widths)
            self.register_buffer("offsets", offset)

    def forward(self, property):
        """Compute expanded gaussian property values.
        Args:
            property (torch.Tensor): property values of (N_b x 1) shape.
        Returns:
            torch.Tensor: layer output of (N_b x N_g) shape.
        """
        # compute width of Gaussian functions (using an overlap of 1 STDDEV)
        coeff = -0.5 / torch.pow(self.widths, 2)[None, :]
        # Use advanced indexing to compute the individual components
        diff = property - self.offsets[None, :]
        # compute expanded property values
        return torch.exp(coeff * torch.pow(diff, 2))


# condition vector c
class MultiEmbedding(nn.Module):
    """Concatenate multi-embedding vector
    all sublayer should have a attribute named 'n_out'

        feat1 -> sub_layer1
        feat2 -> sub_layer2

    Returns: z(B, out_dim)
    """

    def __init__(self, cond_keys: list, types: dict, *args, **kwargs):
        """Get each condition vector

        Args:
            cond_keys (list): list of condition name strings
            types (dict or dict-like): kwargs of sub-embedding modules
        """
        super().__init__()
        self.cond_keys = cond_keys

        n_in = 0
        self.sub_emb_list = nn.ModuleList()
        for cond_key in cond_keys:
            sub_emb = hydra.utils.instantiate(types[cond_key])
            self.sub_emb_list.append(sub_emb)
            n_in += sub_emb.n_out

    def forward(self, conditions: dict) -> dict:
        # conditions={'composition': (atom_types, num_atoms), 'cond_name': cond_vals}
        return {
            cond_key: sub_emb(conditions[cond_key])
            for cond_key, sub_emb in zip(self.cond_keys, self.sub_emb_list)
        }


# p(z|c)
class ZGivenC(nn.Module):
    def __init__(
        self,
        zdim,
        mode='concat',
        no_mlp=True,
        hidden_dim=64,
        fc_num_layers=1,
        out_dim=64,
        *args,
        **kwargs,
    ):
        """Aggregate condition vector c with embedding vector z, output z',

        Args:
            zdim (int): Dimension of input embedding vector's dim, z_dim
            mode (str, optional): Aggregate mode. ['concatenate', 'bias',
            'scale', 'film'] Defaults to 'concat'.
        """
        super().__init__()
        if mode.startswith('concat') or mode.startswith('cat'):
            self.cond_model = ConcatConditioning()
        elif mode.startswith('bias'):
            self.cond_model = BiasConditioning(zdim)
        elif mode.startswith('scal'):
            self.cond_model = ScaleConditioning(zdim)
        elif mode.startswith('film'):
            self.cond_model = FiLM(zdim)
        else:
            raise ValueError("Unknown mode")

        if no_mlp:
            self.mlp = nn.Identity()
        else:
            self.mlp = build_mlp(None, hidden_dim, fc_num_layers, out_dim)

    def forward(self, z, c: dict):  # return cond_z
        z = self.cond_model(z, list(c.values()))
        z = self.mlp(z)
        return z


class ConcatConditioning(nn.Module):
    """z = [z|c1|c2|...]"""

    def __init__(self):
        super().__init__()

    def forward(self, z, c: list):
        z = torch.cat([z] + c, axis=-1)
        return z


class BiasConditioning(nn.Module):
    """z = z + Lin([c1|c2])"""

    def __init__(self, zdim):
        super().__init__()
        self.linear = nn.LazyLinear(zdim)

    def forward(self, z, c: list):
        z = x + self.linear(torch.cat(c, axis=-1))
        return z


class ScaleConditioning(nn.Module):
    """z = z * Lin([c1|c2])"""

    def __init__(self, zdim):
        super().__init__()
        self.linear = nn.LazyLinear(zdim)

    def forward(self, z, c: list):
        z = x * self.linear(torch.cat(c, axis=-1))
        return z


class FiLM(nn.Module):
    """z = γ(y) * x + β(y)"""

    def __init__(self, zdim):
        super().__init__()
        self.gamma = nn.LazyLinear(zdim)
        self.beta = nn.LazyLinear(zdim)

    def forward(self, z, c: list):
        c = torch.cat(c, axis=-1)
        z = self.gamma(c) * z + self.beta(c)
        return z


if __name__ == '__main__':
    bs, xdim, ydim = 16, 32, 6
    film_cond = FiLM(xdim, ydim)
    x = torch.rand(bs, xdim)
    y = torch.rand(bs, ydim)
    z = film_cond(x, y)
    print('x shape : ', x.shape)
    print('y shape : ', y.shape)
    print('z shape : ', z.shape)
