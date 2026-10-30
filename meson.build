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
python = find_program('python3')
test('eal-affinity', python, args: [meson.project_source_root() / 'tests/eal_args_test.py'])
common_sources = ['common/common.c', 'core/worker.c', 'core/transmit.c']
dpds_exe = executable('dpds', ['dpds.c', 'core/receive.c', 'core/combined.c'] + common_sources,
  dependencies: [dpdk, core], build_rpath: meson.project_source_root() / 'dpds-core/target/release', install: true)
foreach name: ['worker', 'tx', 'lifecycle', 'reorder', 'tx_trace', 'ledger', 'rings', 'recovery', 'split', 'rx_split', 'tx_pending', 'tx_burst', 'tx_burst_cases']
  runner = executable(name + '-test', ['tests/' + name + '.c', 'core/receive.c'] + common_sources,
    dependencies: [dpdk, core], c_args: ['-UNDEBUG'],
    build_rpath: meson.project_source_root() / 'dpds-core/target/release')
  args = [meson.project_source_root() / 'tests/eal_args.py', runner.full_path(), '--no-pci', '--no-huge', '--no-telemetry', '--in-memory', '--file-prefix=hng-' + name]
  if name == 'rings'
    args += ['--vdev=net_null0,no-rx=1', '--vdev=net_null1,no-rx=1']
  endif
  if name == 'tx_pending'
    args += ['--vdev=net_null0,no-rx=1']
  endif
  if name == 'rx_split'
    args += ['--vdev=net_null0', '--vdev=net_null1']
  endif
  test(name, python, args: args, depends: runner,
    workdir: meson.project_source_root() / 'tests', timeout: 30, is_parallel: false)
endforeach

test('null-layouts', python, args: [meson.project_source_root() / 'tests/null_smoke.py', dpds_exe],
  workdir: meson.project_build_root(), timeout: 60, is_parallel: false)

# Software-only integrity: actual PMDs, synthetic payloads, independent oracle.
threads = dependency('threads')
ring_pmd = meson.get_compiler('c').find_library('rte_net_ring', dirs: dpdk.get_variable(pkgconfig: 'libdir'), required: true)
io_oracle = executable('io-oracle-test', ['tests/io_oracle_test.c', 'tests/io_oracle.c'], c_args: ['-UNDEBUG'])
test('io-oracle', io_oracle)
io_pmd = executable('io-pmd-test', ['tests/io_pmd.c', 'tests/io_oracle.c', 'core/receive.c'] + common_sources,
  dependencies: [dpdk, core, threads, ring_pmd], c_args: ['-UNDEBUG'],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
foreach mode: ['pcap', 'ring', 'ring-stepped', 'ring-split', 'ring-stepped-split', 'ring-io-split', 'ring-stepped-io-split', 'ring-io-split-lcores']
  test('io-' + mode, python, args: [meson.project_source_root() / 'tests/io_pmd.py', mode, io_pmd],
    timeout: 300, is_parallel: false)
endforeach
test('io-af-packet', python, args: [meson.project_source_root() / 'tests/io_pmd.py', 'af_packet', dpds_exe],
  timeout: 30, is_parallel: false)

# Host packet-rate benchmark over net_ring; the test is a short smoke run.
ring_rate = executable('hng-ring-rate', ['perf/ring_rate.c', 'core/receive.c'] + common_sources,
  dependencies: [dpdk, core, ring_pmd],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('perf-ring-rate', python, args: [meson.project_source_root() / 'perf/ring_rate.py', ring_rate],
  timeout: 60, is_parallel: false)
test('perf-ring-rate-split', python, args: [meson.project_source_root() / 'perf/ring_rate.py', '--split', ring_rate],
  timeout: 60, is_parallel: false)
test('perf-ring-rate-io-split', python, args: [meson.project_source_root() / 'perf/ring_rate.py', '--io-split', ring_rate,
  '--', '-d', '0.2', '-w', '0.05', '-n', '2046'],
  timeout: 60, is_parallel: false)
pool_calc = executable('hng-pool-calc', ['perf/pool_calc.c', 'common/common.c'],
  dependencies: [dpdk, core], link_args: ['-lm'],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('perf-pool-calc', python, args: [meson.project_source_root() / 'perf/pool_calc_test.py', pool_calc],
  timeout: 60, is_parallel: false)
configure_file(input: 'perf/sched_bench.json', output: 'sched_bench.json', copy: true)
configure_file(input: 'perf/sched_bench_prod.json', output: 'sched_bench_prod.json', copy: true)
sched_bench = executable('hng-sched-bench', ['perf/sched_bench.c'] + common_sources,
  dependencies: [dpdk, core],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('perf-sched-bench', python, args: [meson.project_source_root() / 'perf/sched_bench.py', sched_bench,
  '--', '-n', '50000', '-w', '0', '-r', '1'], workdir: meson.project_build_root(), timeout: 60, is_parallel: false)
