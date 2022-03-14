import torch
import torch.nn.functional as F
from torch import nn, einsum

from scipy.fftpack import next_fast_len
from einops import rearrange, repeat
from einops.layers.torch import Rearrange

# helper functions

def exists(val):
    return val is not None

# classes

def InputEmbedding(time_features, model_dim, kernel_size = 3, dropout = 0.):
    return nn.Sequential(
        Rearrange('b n d -> b d n'),
        nn.Conv1d(time_features, model_dim, kernel_size = kernel_size, padding = kernel_size // 2),
        nn.Dropout(dropout),
        Rearrange('b d n -> b n d'),
    )

def FeedForward(dim, mult = 4, dropout = 0.):
    return nn.Sequential(
        nn.Linear(dim, dim * mult),
        nn.Sigmoid(),
        nn.Dropout(dropout),
        nn.Linear(dim * mult, dim),
        nn.Dropout(dropout)
    )

class FeedForwardBlock(nn.Module):
    def __init__(
        self,
        *,
        dim,
        **kwargs
    ):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.ff = FeedForward(dim, **kwargs)

    def forward(self, x):
        return self.norm(x + self.ff(x))

# encoder related classes

## multi-head exponential smoothing attention

def conv1d_fft(x, weights, dim = -2, weight_dim = -1):
    # Algorithm 3 in paper

    N = x.shape[dim]
    M = weights.shape[weight_dim]

    fast_len = next_fast_len(N + M - 1)

    f_x = torch.fft.rfft(x, n = fast_len, dim = dim)
    f_weight = torch.fft.rfft(weights, n = fast_len, dim = weight_dim)

    f_v_weight = f_x * rearrange(f_weight.conj(), '... -> ... 1')
    out = torch.fft.irfft(f_v_weight, fast_len, dim = dim)
    out = out.roll(-1, dims = (dim,))

    indices = torch.arange(start = fast_len - N, end = fast_len, dtype = torch.long, device = x.device)
    out = out.index_select(dim, indices)
    return out

class MHESA(nn.Module):
    def __init__(
        self,
        *,
        dim,
        heads = 8,
        dim_head = 32,
        dropout = 0.
    ):
        super().__init__()
        inner_dim = heads * dim_head
        self.heads = heads
        self.initial_state = nn.Parameter(torch.randn(heads, dim_head))

        self.dropout = nn.Dropout(dropout)
        self.alpha = nn.Parameter(torch.randn(heads))

        self.project_in = nn.Linear(dim, inner_dim)
        self.project_out = nn.Linear(inner_dim, dim)

    def naive_Aes(self, x, weights):
        n, h = x.shape[-2], self.heads

        # in appendix A.1 - Algorithm 2

        arange = torch.arange(n, device = x.device)

        weights = repeat(weights, '... l -> ... t l', t = n)
        indices = repeat(arange, 'l -> h t l', h = h, t = n)

        indices = (indices - rearrange(arange + 1, 't -> 1 t 1')) % n

        weights = weights.gather(-1, indices)
        weights = self.dropout(weights)

        # causal

        weights = weights.tril()

        # multiply

        output = einsum('b h n d, h m n -> b h m d', x, weights)
        return output

    def forward(self, x, naive = False):
        b, n, d, h, device = *x.shape, self.heads, x.device

        # linear project in

        x = self.project_in(x)

        # split out heads

        x = rearrange(x, 'b n (h d) -> b h n d', h = h)

        # temporal difference

        x = torch.cat((
            repeat(self.initial_state, 'h d -> b h 1 d', b = b),
            x
        ), dim = -2)

        x = x[:, :, 1:] - x[:, :, :-1]

        # prepare exponential alpha

        alpha = self.alpha.sigmoid()
        alpha = rearrange(alpha, 'h -> h 1')

        # arange == powers

        arange = torch.arange(n, device = device)
        weights = alpha * (1 - alpha) ** torch.flip(arange, dims = (0,))

        if naive:
            output = self.naive_Aes(x, weights)
        else:
            output = conv1d_fft(x, weights)

        # get initial state contribution

        init_weight = (1 - alpha) ** (arange + 1)
        init_output = rearrange(init_weight, 'h n -> h n 1') * rearrange(self.initial_state, 'h d -> h 1 d')

        output = output + init_output

        # merge heads

        output = rearrange(output, 'b h n d -> b n (h d)')
        return self.project_out(output)

## frequency attention

class FrequencyAttention(nn.Module):
    def __init__(
        self,
        *,
        K = 4,
        dropout = 0.
    ):
        super().__init__()
        self.K = K
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        freqs = torch.fft.rfft(x, dim = 1)

        # get amplitudes

        amp = freqs.abs()
        amp = self.dropout(amp)

        # topk amplitudes - for seasonality, branded as attention

        _, topk_amp_indices = amp.topk(k = self.K, dim = 1)

        # scatter back

        topk_freqs = torch.zeros_like(freqs).scatter(1, topk_amp_indices, freqs)

        # inverse fft

        return torch.fft.irfft(topk_freqs, dim = 1)

## level module

class Level(nn.Module):
    def __init__(self, time_features, model_dim):
        super().__init__()
        self.alpha = nn.Parameter(torch.Tensor([0.]))
        self.to_growth = nn.Linear(model_dim, time_features)
        self.to_seasonal = nn.Linear(model_dim, time_features)

    def forward(self, x, latent_growth, latent_seasonal):
        # following equation in appendix A.2

        n, device = x.shape[1], x.device

        alpha = self.alpha.sigmoid()

        arange = torch.arange(n, device = device)
        powers = torch.flip(arange, dims = (0,))

        # Aes for raw time series signal with seasonal terms (from frequency attention) subtracted out

        seasonal =self.to_seasonal(latent_seasonal)
        Aes_weights = alpha * (1 - alpha) ** powers
        seasonal_normalized_term = conv1d_fft(x - seasonal, Aes_weights)

        # auxiliary term

        growth = self.to_growth(latent_growth)
        growth_smoothing_weights = (1 - alpha) ** powers
        growth_term = conv1d_fft(x, Aes_weights)

        return seasonal_normalized_term + growth_term

# decoder classes

class LevelStack(nn.Module):
    def forward(self, x, num_steps_forecast):
        return repeat(x[:, -1], 'b d -> b n d', n = num_steps_forecast)

# main class

class ETSFormer(nn.Module):
    def __init__(
        self,
        *,
        time_features,
        model_dim,
        embed_kernel_size = 3,
        layers = 2,
        heads = 8,
        dim_head = 32,
        K = 4,
        dropout = 0.
    ):
        super().__init__()
        self.embed = InputEmbedding(time_features, model_dim, kernel_size = embed_kernel_size, dropout = dropout)

        self.encoder_layers = nn.ModuleList([])

        for ind in range(layers):
            is_last_layer = (ind - 1) == layers

            self.encoder_layers.append(nn.ModuleList([
                FrequencyAttention(K = K, dropout = dropout),
                MHESA(dim = model_dim, dim_head = dim_head, heads = heads, dropout = dropout),
                nn.LayerNorm(model_dim),
                FeedForwardBlock(dim = model_dim) if not is_last_layer else None,
                Level(time_features = time_features, model_dim = model_dim)
            ]))

        self.level_stack = LevelStack()

    def forward(self, x, *, num_steps_forecast):
        z = self.embed(x)

        for freq_attn, mhes_attn, attn_post_ln, ff_block, level in self.encoder_layers:
            latent_seasonal = freq_attn(z)
            z = z - latent_seasonal

            latent_growth = mhes_attn(z)
            z = z -latent_growth

            z = attn_post_ln(z)

            if exists(ff_block):
                z = ff_block(z)

            x = level(x, latent_seasonal, latent_growth)

        level = self.level_stack(x, num_steps_forecast = num_steps_forecast)
        return level
