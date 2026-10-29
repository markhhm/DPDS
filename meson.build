project('dpds', 'c', version: '0.2', meson_version: '>=0.56.0',
  default_options: ['warning_level=3', 'buildtype=release', 'b_ndebug=false'])
dpdk = dependency('libdpdk', required: true)
configure_file(input: 'config.json', output: 'config.json', copy: true)
cargo_build = custom_target('dpds-core', output: 'libdpds_core.stamp',
  command: ['sh', '-c', 'cargo build --locked --release --manifest-path "' + meson.project_source_root() / 'dpds-core/Cargo.toml' + '" && touch @OUTPUT@'],
  build_by_default: true, build_always_stale: true, console: true)
core = declare_dependency(
  link_args: ['-L' + meson.project_source_root() / 'dpds-core/target/release', '-ldpds_core'],
  compile_args: ['-I' + meson.project_source_root() / 'dpds-core/target'], sources: cargo_build)
common_sources = ['common/common.c', 'core/worker.c', 'core/transmit.c']
dpds_exe = executable('dpds', ['dpds.c', 'core/receive.c', 'core/combined.c'] + common_sources,
  dependencies: [dpdk, core], build_rpath: meson.project_source_root() / 'dpds-core/target/release', install: true)
foreach name: ['worker', 'tx', 'lifecycle', 'reorder', 'tx_trace', 'ledger', 'rings', 'recovery']
  runner = executable(name + '-test', ['tests/' + name + '.c'] + common_sources,
    dependencies: [dpdk, core], c_args: ['-UNDEBUG'],
    build_rpath: meson.project_source_root() / 'dpds-core/target/release')
  args = ['-l', '0', '--no-pci', '--no-huge', '--no-telemetry', '--file-prefix=hng-' + name]
  if name == 'rings'
    args += ['--vdev=net_null0,no-rx=1', '--vdev=net_null1,no-rx=1']
  endif
  test(name, runner, args: args,
    workdir: meson.project_source_root() / 'tests', timeout: 30, is_parallel: false)
endforeach

python = find_program('python3')
test('null-layouts', python, args: [meson.project_source_root() / 'tests/null_smoke.py', dpds_exe],
  workdir: meson.project_build_root(), timeout: 60, is_parallel: false)
