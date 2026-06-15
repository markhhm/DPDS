FROM ubuntu:26.04

ARG DPDK_VERSION=26.03
ARG RUST_VERSION=1.96

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# System dependencies for DPDK build + the shell environment
RUN apt-get update && apt-get install -y --no-install-recommends \
  build-essential \
  meson \
  ninja-build \
  pkg-config \
  python3 \
  python3-pyelftools \
  libnuma-dev \
  libpcap-dev \
  linux-headers-generic \
  git \
  curl \
  ca-certificates \
  libclang-dev \
  llvm-dev \
  clang \
  locales \
  && locale-gen en_US.UTF-8 \
  && update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 \
  && rm -rf /var/lib/apt/lists/*

# Build and install DPDK from source
RUN curl -fsSL https://fast.dpdk.org/rel/dpdk-${DPDK_VERSION}.tar.xz | tar -xJ \
  && cd dpdk-${DPDK_VERSION} \
  && meson setup build -Dplatform=generic \
  && cd build \
  && ninja \
  && ninja install \
  && ldconfig \
  && cd ../.. \
  && rm -rf dpdk-${DPDK_VERSION}

# Install Rust toolchain with rustfmt and clippy
# rust-analyzer is omitted here — the VS Code extension bundles its own binary,
# and the rustup component is unavailable on some platforms (e.g. aarch64).
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
  | sh -s -- -y --default-toolchain ${RUST_VERSION}
ENV PATH="/root/.cargo/bin:${PATH}"
RUN find /root/.cargo/bin -maxdepth 1 -type f \
  -exec ln -sf {} /usr/local/bin/ \;

# Bindgen needs to find DPDK headers via pkg-config
ENV PKG_CONFIG_PATH="/usr/local/lib/aarch64-linux-gnu/pkgconfig:/usr/local/lib/x86_64-linux-gnu/pkgconfig"
