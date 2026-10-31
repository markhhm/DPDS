project('dpds', 'c', version: '0.2', meson_version: '>=0.56.0',
  default_options: ['warning_level=3', 'buildtype=release', 'b_ndebug=false'])
dpdk = dependency('libdpdk', required: true)
configure_file(input: 'config.json', output: 'config.json', copy: true)
cargo_build = custom_target('dpds-core', output: 'libdpds_core.stamp',
  command: ['sh', '-c', 'cargo build --locked --release --lib --examples --manifest-path "' + meson.project_source_root() / 'dpds-core/Cargo.toml' + '" && touch @OUTPUT@'],
  build_by_default: true, build_always_stale: true, console: true)
core = declare_dependency(
  link_args: ['-L' + meson.project_source_root() / 'dpds-core/target/release', '-ldpds_core'],
  compile_args: ['-I' + meson.project_source_root() / 'dpds-core/target'], sources: cargo_build)
python = find_program('python3')
test('eal-affinity', python, args: [meson.project_source_root() / 'tests/eal_args_test.py'])
threads = dependency('threads')
common_sources = ['common/observer.c', 'common/common.c', 'core/worker.c', 'core/transmit.c']
dpds_exe = executable('dpds', ['dpds.c', 'core/receive.c', 'core/combined.c'] + common_sources,
  dependencies: [dpdk, core, threads], build_rpath: meson.project_source_root() / 'dpds-core/target/release', install: true)
foreach name: ['worker', 'tx', 'lifecycle', 'reorder', 'tx_trace', 'ledger', 'rings', 'recovery', 'split', 'rx_split', 'tx_pending', 'tx_burst', 'tx_burst_cases', 'observer', 'observer_split', 'observer_snapshot']
  runner = executable(name + '-test', ['tests/' + name + '.c', 'core/receive.c'] + common_sources,
    dependencies: [dpdk, core, threads], c_args: ['-UNDEBUG'],
    link_args: name == 'tx_trace' ? ['-Wl,--wrap=host_now_ns'] : [],
    # LTO can resolve the clock internally before the linker applies --wrap.
    override_options: name == 'tx_trace' ? ['b_lto=false'] : [],
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

test('g2-software', python,
  args: [meson.project_source_root() / 'scripts/run-unittest-groups.py',
         'tools.g2.tests.test_online', '--', 'tools.g2.tests.test_reference',
         'tools.g2.tests.test_report', 'tools.g2.tests.test_probe', 'tools.g2.tests.test_differential',
         'tools.g2.tests.test_protocol', 'tools.g2.tests.test_runner',
         'tools.g2.tests.test_calibration', 'tools.g2.tests.test_custody',
         'tools.g2.tests.test_reference_v1', 'tools.g2.tests.test_registration',
         'tools.g2.tests.test_counters', 'tools.g2.tests.test_identity_index',
         'tools.g2.tests.test_bundle', 'tools.g2.tests.test_serializer',
         'tools.g2.tests.test_live_custody', 'tools.g2.tests.test_matrix'],
  workdir: meson.project_source_root(), timeout: 180, is_parallel: false)
# Software-only integrity: actual PMDs, synthetic payloads, independent oracle.
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
pool_calc = executable('hng-pool-calc', ['perf/pool_calc.c', 'common/common.c', 'common/observer.c'],
  dependencies: [dpdk, core, threads], link_args: ['-lm'],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('perf-pool-calc', python, args: [meson.project_source_root() / 'perf/pool_calc_test.py', pool_calc],
  timeout: 60, is_parallel: false)
configure_file(input: 'perf/sched_bench.json', output: 'sched_bench.json', copy: true)
configure_file(input: 'perf/sched_bench_prod.json', output: 'sched_bench_prod.json', copy: true)
sched_bench = executable('hng-sched-bench', ['perf/sched_bench.c'] + common_sources,
  dependencies: [dpdk, core, threads],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('perf-sched-bench', python, args: [meson.project_source_root() / 'perf/sched_bench.py', sched_bench,
  '--', '-n', '50000', '-w', '0', '-r', '1'], workdir: meson.project_build_root(), timeout: 60, is_parallel: false)

test('observer-export', python, args: [meson.project_source_root() / 'tests/observer_export.py', dpds_exe],
  timeout: 90, is_parallel: false)
test('observer-adapter', python, args: [meson.project_source_root() / 'tests/observer_adapter.py'],
  timeout: 30, is_parallel: false)

test('m6-smoke', python, args: [meson.project_source_root() / 'tests/m6_smoke.py', dpds_exe],
  workdir: meson.project_source_root(), timeout: 30, is_parallel: false)
