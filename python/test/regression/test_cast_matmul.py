"""
Mixed precision tests for matmul (tl.dot) with cast (tl.to)

issue: https://github.com/triton-lang/triton/issues/2523

TODO: float8 types
"""

import pytest
import torch

import triton
import triton.language as tl
from triton._internal_testing import is_hip_cdna3, is_cuda, is_hip

input_dtypes = ["bfloat16", "float16", "float32"]
if is_cuda():
    input_dtypes += ["int8", "float8_e5m2"]
    cc = torch.cuda.get_device_capability(0)
    if cc >= (8, 9):
        input_dtypes += ["float8_e4m3fn"]
elif is_hip_cdna3():
    input_dtypes += [
        "int8",
        "float8_e5m2",
        # natively supported on CDNA3 (see CDNA3 ISA, section 7.2)
        "float8_e4m3fnuz",
    ]

out_dtypes = ["float16", "float32"]


@triton.jit
def matmul_kernel(A, B, C, M, N, K,  #
                  stride_am, stride_ak,  #
                  stride_bk, stride_bn,  #
                  stride_cm, stride_cn,  #
                  compute_dtype: tl.constexpr, BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr,  #
                  BLOCK_K: tl.constexpr, GROUP_M: tl.constexpr):
    # matrix multiplication
    pid = tl.program_id(0)
    grid_m = tl.cdiv(M, BLOCK_M)
    grid_n = tl.cdiv(N, BLOCK_N)
    # re-order program ID for better L2 performance
    width = GROUP_M * grid_n
    group_id = pid // width
    group_size = min(grid_m - group_id * GROUP_M, GROUP_M)
    pid_m = group_id * GROUP_M + (pid % group_size)
    pid_n = (pid % width) // (group_size)
    # do matrix multiplication
    rm = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    rn = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    ram = tl.max_contiguous(tl.multiple_of(rm % M, BLOCK_M), BLOCK_M)
    rbn = tl.max_contiguous(tl.multiple_of(rn % N, BLOCK_N), BLOCK_N)
    rk = tl.arange(0, BLOCK_K)
    # pointers
    A = A + (ram[:, None] * stride_am + rk[None, :] * stride_ak)
    B = B + (rk[:, None] * stride_bk + rbn[None, :] * stride_bn)
    acc_dtype = tl.float16 if compute_dtype == tl.float16 and C.dtype.element_ty == tl.float16 else tl.float32
    acc = tl.zeros((BLOCK_M, BLOCK_N), dtype=acc_dtype)
    for k in range(0, tl.cdiv(K, BLOCK_K)):
        k_remaining = K - k * BLOCK_K
        _0 = tl.zeros((1, 1), dtype=compute_dtype)
        a = tl.load(A, mask=rk[None, :] < k_remaining, other=_0)
        b = tl.load(B, mask=rk[:, None] < k_remaining, other=_0)
        acc += tl.dot(a.to(compute_dtype), b.to(compute_dtype), out_dtype=acc_dtype)
        A += BLOCK_K * stride_ak
        B += BLOCK_K * stride_bk
    acc = acc.to(C.dtype.element_ty)
    # rematerialize rm and rn to save registers
    rm = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    rn = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    C = C + (rm[:, None] * stride_cm + rn[None, :] * stride_cn)
    mask = (rm < M)[:, None] & (rn < N)[None, :]
    tl.store(C, acc, mask=mask)


@pytest.mark.parametrize("M, K, N, BLOCK_K, BLOCK_M, BLOCK_N, w_dtype, x_dtype, out_dtype",
                         [(M, K, N, BLOCK_K, BLOCK_M, BLOCK_N, w, x, o)  #
                          for BLOCK_K in [16, 32, 64]  #
                          for BLOCK_M in [16, 64]  #
                          for BLOCK_N in [16, 64, 128]  #
                          for (M, K, N) in [(768, 768, 1024)]  #
                          for w in input_dtypes
                          for x in input_dtypes  #
                          for o in out_dtypes])
def test_cast_matmul(M, K, N, BLOCK_K, BLOCK_M, BLOCK_N, w_dtype, x_dtype, out_dtype, device):
    if is_hip() and (BLOCK_K, BLOCK_M, BLOCK_N) in ((64, 64, 128), (64, 16, 128)):
        pytest.skip("skip as they run out of shared memory")
    if x_dtype == w_dtype:
        pytest.skip("skip the same input dtype")
    x_dtype: torch.dtype = getattr(torch, x_dtype)
    w_dtype: torch.dtype = getattr(torch, w_dtype)

    def init_tensor(dtype, shape):
        if dtype == torch.int8:
            return torch.randint(0, 2, shape, device=device, dtype=dtype)
        elif dtype in (torch.float8_e4m3fn, torch.float8_e4m3fnuz, torch.float8_e5m2):
            return torch.randn(shape, device=device, dtype=torch.float16).to(dtype)
        else:
            return torch.randn(shape, device=device, dtype=dtype)

    def compute_dtype(a_dtype, b_dtype):
        # a holds the larger dtype
        if a_dtype.itemsize < b_dtype.itemsize:
            a_dtype, b_dtype = b_dtype, a_dtype
        # float64 matmul is not supported by triton
        if a_dtype == torch.float64:
            return torch.float32
        # If they are both 1 byte or float16 and (1 byte or float16)
        if a_dtype.itemsize == 1 or (a_dtype == torch.float16 and b_dtype != torch.bfloat16):
            return torch.float16
        else:
            return torch.float32

    # nasty hack
    def get_triton_dtype(dtype):
        return getattr(tl, str(dtype).removeprefix("torch."))

    torch.manual_seed(42)
    a = init_tensor(w_dtype, (M, K))
    b = init_tensor(x_dtype, (K, N))

    torch_dtype = getattr(torch, out_dtype)
    out_torch = torch.matmul(a.to(torch_dtype), b.to(torch_dtype))
    out_triton = torch.empty((M, N), device=device, dtype=torch_dtype)
    compute_triton = get_triton_dtype(compute_dtype(w_dtype, x_dtype))

    # launch kernel
    block_m, block_n, block_k = BLOCK_M, BLOCK_N, BLOCK_K
    grid = ((triton.cdiv(M, block_m) * triton.cdiv(N, block_n)), 1)

    matmul_kernel[grid](
        a, b, out_triton, M, N, K,  #
        a.stride(0), a.stride(1),  #
        b.stride(0), b.stride(1),  #
        out_triton.stride(0), out_triton.stride(1),  #
        compute_triton, GROUP_M=8,  #
        BLOCK_M=block_m,  #
        BLOCK_N=block_n,  #
        BLOCK_K=block_k)

    torch.testing.assert_close(out_torch, out_triton, atol=0.3, rtol=0.01)
