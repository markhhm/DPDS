project('dpds', 'c',
  version : '0.1',
  meson_version : '>=0.56.0',
  default_options : [
    'warning_level=3',
    'buildtype=release',
    'optimization=3',
    'b_lto=true',
    'b_ndebug=true'
  ])

dpdk = dependency('libdpdk', required: true)

# Copy config.json to build folder
configure_file(
  input: 'config.json',
  output: 'config.json',
  copy: true
)

# Build Rust dpds-core library
cargo_build = custom_target(
  'dpds-core',
  output: 'libdpds_core.stamp',
  command: [
    'sh', '-c',
    'RUSTFLAGS="-C target-cpu=native" cargo build --release --manifest-path ' + meson.project_source_root() / 'dpds-core/Cargo.toml' + ' && touch @OUTPUT@'
  ],
  build_by_default: true,
  build_always_stale: true,
  console: true,
  install: false
)

# Declare the Rust library
dpds_core_lib = declare_dependency(
  link_args: ['-L' + meson.project_source_root() / 'dpds-core/target/release', '-ldpds_core'],
  compile_args: ['-I' + meson.project_source_root() / 'dpds-core/target'],
  sources: cargo_build
)

# Use -march=native to enable CPU-specific optimizations (SSE4, AVX, etc.)
c_native_args = ['-march=native', '-funroll-loops']

exe = executable(
  'dpds',
  ['dpds.c', 'common/common.c', 'core/receive.c', 'core/worker.c', 'core/transmit.c', 'core/combined.c'],
  c_args: c_native_args,
  dependencies: [dpdk, dpds_core_lib],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release',
  install : true)

test('basic', exe)
