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
seqcheck = executable('seqcheck', 'tools/seqcheck/seqcheck.c',
  dependencies: [dependency('libcrypto'), dependency('threads')], install: true)
test('seqcheck', python, args: ['-m', 'unittest', '-v', 'tools.seqcheck.test_seqcheck'],
  env: ['SEQCHECK_BINARY=' + seqcheck.full_path()],
  workdir: meson.project_source_root(), depends: seqcheck)
test('eal-affinity', python, args: [meson.project_source_root() / 'tests/eal_args_test.py'])
test('bringup-check', python, args: [meson.project_source_root() / 'tests/bringup_check.py'],
  workdir: meson.project_source_root(), timeout: 30)
test('trex-control', python, args: [meson.project_source_root() / 'tests/trex_control_test.py'],
  workdir: meson.project_source_root(), timeout: 30)
test('bprime-stl-profile', python, args: [meson.project_source_root() / 'tests/bprime_stl_profile.py'],
  workdir: meson.project_source_root(), timeout: 30)
core_pcap = executable('core-pcap-replay', 'dpds-core/scripts/pcap_trace_replay.c',
  dependencies: [dpdk, core], c_args: ['-UNDEBUG'],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('core-pcap-replay', python, args: [meson.project_source_root() / 'tests/core_pcap_replay.py'],
  env: ['HNG_PCAP_BINARY=' + core_pcap.full_path()], depends: core_pcap,
  workdir: meson.project_source_root() / 'dpds-core', timeout: 180, is_parallel: false)
threads = dependency('threads')
common_sources = ['common/observer.c', 'common/observer_binary.c', 'common/common.c', 'common/diagnostics.c', 'common/storage.c', 'common/storage_hybrid.c', 'common/storage_engine.c', 'common/storage_host.c', 'core/worker.c', 'core/transmit.c']
hybrid_proto = executable('hng-storage-hybrid', ['perf/storage_hybrid.c', 'common/storage.c', 'common/storage_hybrid.c'],
  dependencies: [dpdk, core], c_args: ['-UNDEBUG'],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
m7_engine_test = executable('m7-engine-test', ['tests/m7_engine.c'] + common_sources,
  dependencies: [dpdk, core, threads], c_args: ['-UNDEBUG'],
  link_args: ['-Wl,--wrap=host_now_ns', '-Wl,--wrap=hng_lines_pop_merged', '-Wl,--wrap=hng_head_pop'], override_options: ['b_lto=false'],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('m7-engine', python, args: [meson.project_source_root() / 'tests/eal_args.py', m7_engine_test,
  '--no-pci', '--no-huge', '--no-telemetry', '--in-memory', '-m', '256',
  '--vdev=net_null0,no-rx=1', '--vdev=net_null1,no-rx=1', '--file-prefix=hng-m7-engine'],
  workdir: meson.project_source_root() / 'tests', timeout: 30, is_parallel: false)
test('storage-hybrid-capacity', python, args: [meson.project_source_root() / 'perf/storage_hybrid_capacity_test.py'],
  workdir: meson.project_source_root(), timeout: 30)
test('storage-hybrid-smoke', python, args: [meson.project_source_root() / 'perf/storage_hybrid.py', hybrid_proto,
  '--m7-opt-in', '--thresholds', '512', '--trials', '1'],
  workdir: meson.project_source_root(), timeout: 30, is_parallel: false)
hybrid_test = executable('storage-hybrid-test', ['tests/storage_hybrid.c', 'common/storage.c', 'common/storage_hybrid.c'],
  dependencies: [dpdk, core], c_args: ['-UNDEBUG'])
test('storage-hybrid', python, args: [meson.project_source_root() / 'tests/eal_args.py', hybrid_test,
  '--no-pci', '--no-huge', '--no-telemetry', '--in-memory', '--file-prefix=hng-storage-hybrid'],
  workdir: meson.project_source_root(), timeout: 30, is_parallel: false)
storage_proto = executable('hng-storage-proto', ['perf/storage_proto.c', 'common/storage.c'],
  dependencies: [dpdk, core], c_args: ['-UNDEBUG'],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('storage-capacity', python, args: [meson.project_source_root() / 'perf/storage_capacity_test.py'],
  workdir: meson.project_source_root(), timeout: 30)
test('storage-proto-smoke', python, args: [meson.project_source_root() / 'perf/storage_proto.py', storage_proto,
  '--trials', '1'], workdir: meson.project_source_root(), timeout: 30, is_parallel: false)
storage_test = executable('storage-test', ['tests/storage.c', 'common/storage.c'],
  dependencies: [dpdk, core], c_args: ['-UNDEBUG'])
test('storage', python, args: [meson.project_source_root() / 'tests/eal_args.py', storage_test,
  '--no-pci', '--no-huge', '--no-telemetry', '--in-memory', '--file-prefix=hng-storage'],
  workdir: meson.project_source_root(), timeout: 30, is_parallel: false)
m7_worker = executable('m7-worker-test', ['tests/m7_worker.c'] + common_sources,
  dependencies: [dpdk, core, threads], c_args: ['-UNDEBUG'], link_args: ['-Wl,--wrap=host_now_ns'],
  override_options: ['b_lto=false'], build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('m7-worker', python, args: [meson.project_source_root() / 'tests/eal_args.py', m7_worker,
  '--no-pci', '--no-huge', '--no-telemetry', '--in-memory', '-m', '256', '--file-prefix=hng-m7-worker'],
  workdir: meson.project_source_root() / 'tests', timeout: 30, is_parallel: false)
test('m7-baseline-semantics', python, args: [meson.project_source_root() / 'tests/m7_semantics.py', m7_worker, m7_engine_test],
  workdir: meson.project_source_root() / 'tests', timeout: 60, is_parallel: false)
backend_test = executable('m7-backend-test', ['tests/storage_engine.c', 'common/storage_engine.c', 'common/storage.c', 'common/storage_hybrid.c'],
  dependencies: [dpdk, core], c_args: ['-UNDEBUG'])
test('m7-backend', python, args: [meson.project_source_root() / 'tests/eal_args.py', backend_test,
  '--no-pci', '--no-huge', '--no-telemetry', '--in-memory', '--file-prefix=hng-m7-backend'], timeout: 30, is_parallel: false)
dpds_exe = executable('dpds', ['dpds.c', 'core/receive.c', 'core/combined.c'] + common_sources,
  dependencies: [dpdk, core, threads], build_rpath: meson.project_source_root() / 'dpds-core/target/release', install: true)
foreach name: ['epoch_anchor', 'handoff', 'worker', 'tx', 'lifecycle', 'reorder', 'tx_trace', 'ledger', 'rings', 'recovery', 'split', 'rx_split', 'tx_pending', 'tx_burst', 'tx_burst_cases', 'observer', 'observer_split', 'observer_7', 'observer_snapshot']
  clock_wrap_args = []
  if name in ['epoch_anchor', 'tx_trace', 'handoff']
    clock_wrap_args += ['-Wl,--wrap=host_now_ns']
  endif
  if name == 'epoch_anchor'
    clock_wrap_args += ['-Wl,--wrap=clock_gettime']
  endif
  runner = executable(name + '-test', ['tests/' + name + '.c', 'core/receive.c'] + common_sources,
    dependencies: [dpdk, core, threads], c_args: ['-UNDEBUG'],
    link_args: clock_wrap_args,
    # LTO can resolve the clock internally before the linker applies --wrap.
    override_options: name in ['epoch_anchor', 'tx_trace', 'handoff'] ? ['b_lto=false'] : [],
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

test('m7-handoff', python, args: [meson.project_source_root() / 'tests/eal_args.py',
  meson.project_build_root() / 'handoff-test', '--no-pci', '--no-huge', '--no-telemetry', '--in-memory',
  '--file-prefix=hng-m7-handoff', '--', 'm7'], workdir: meson.project_source_root() / 'tests',
  timeout: 30, is_parallel: false)

test('null-layouts', python, args: [meson.project_source_root() / 'tests/null_smoke.py', dpds_exe],
  workdir: meson.project_build_root(), timeout: 60, is_parallel: false)
pool_cleanup = executable('pool-cleanup-fixture', ['dpds.c', 'tests/pool_cleanup.c', 'core/receive.c', 'core/combined.c'] + common_sources,
  dependencies: [dpdk, core, threads], c_args: ['-UNDEBUG'],
  link_args: ['-Wl,--wrap=initialize_port', '-Wl,--wrap=close_port', '-Wl,--wrap=rte_mempool_free', '-Wl,--wrap=host_mark_launched'],
  override_options: ['b_lto=false'],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('pool-cleanup', python, args: [meson.project_source_root() / 'tests/pool_cleanup.py', pool_cleanup],
  workdir: meson.project_build_root(), timeout: 30, is_parallel: false)
test('diagnostics', python, args: [meson.project_source_root() / 'tests/diagnostics.py', dpds_exe],
  workdir: meson.project_build_root(), timeout: 30, is_parallel: false)

# Separate the long event checks; overlap CLI fixtures after differential replay.
test('g2-software', python,
  args: [meson.project_source_root() / 'scripts/run-unittest-groups.py',
         'tools.g2.tests.test_online.OnlineTests',
         'tools.g2.tests.test_reference', 'tools.g2.tests.test_m7_storage', 'tools.g2.tests.test_probe',
         'tools.g2.tests.test_protocol', 'tools.g2.tests.test_runner',
         'tools.g2.tests.test_calibration', 'tools.g2.tests.test_custody',
         'tools.g2.tests.test_reference_v1', 'tools.g2.tests.test_registration',
         'tools.g2.tests.test_counters', 'tools.g2.tests.test_identity_index',
         'tools.g2.tests.test_serializer', 'tools.g2.tests.test_live_custody',
         'tools.g2.tests.test_pcapng', '--',
         'tools.g2.tests.test_online.OnlineTimingTests', '--',
         'tools.g2.tests.test_differential',
         'tools.g2.tests.test_report', '--',
         'tools.g2.tests.test_bundle'],
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
  link_args: ['-Wl,--wrap=hng_engine_load'], override_options: ['b_lto=false'],
  dependencies: [dpdk, core, threads, ring_pmd],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('perf-ring-rate', python, args: [meson.project_source_root() / 'perf/ring_rate.py', ring_rate],
  timeout: 60, is_parallel: false)
test('perf-ring-rate-split', python, args: [meson.project_source_root() / 'perf/ring_rate.py', '--split', ring_rate],
  timeout: 60, is_parallel: false)
test('perf-ring-rate-io-split', python, args: [meson.project_source_root() / 'perf/ring_rate.py', '--io-split', ring_rate,
  '--', '-d', '0.2', '-w', '0.05', '-n', '2046'],
  timeout: 60, is_parallel: false)
test('perf-ring-rate-m7', python, args: [meson.project_source_root() / 'tests/m7_rate.py', ring_rate],
  workdir: meson.project_source_root(), timeout: 45, is_parallel: false)
pool_calc = executable('hng-pool-calc', ['perf/pool_calc.c', 'common/common.c', 'common/diagnostics.c', 'common/observer.c', 'common/observer_binary.c', 'common/storage.c', 'common/storage_hybrid.c', 'common/storage_engine.c', 'common/storage_host.c'],
  dependencies: [dpdk, core, threads], link_args: ['-lm'],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('perf-pool-calc', python, args: [meson.project_source_root() / 'perf/pool_calc_test.py', pool_calc],
  timeout: 60, is_parallel: false)
test('perf-pool-calc-m7', python, args: ['-E', meson.project_source_root() / 'perf/pool_calc_m7_test.py', pool_calc],
  workdir: meson.project_source_root(), timeout: 30, is_parallel: false)
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

binary_fixture = executable('observer-binary-fixture', ['tests/observer_binary.c', 'core/receive.c'] + common_sources,
  dependencies: [dpdk, core, threads], c_args: ['-UNDEBUG'],
  link_args: ['-Wl,--wrap=host_now_ns'], override_options: ['b_lto=false'],
  build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('observer-binary', python, args: [meson.project_source_root() / 'tests/native_binary.py', binary_fixture],
  workdir: meson.project_source_root() / 'tests', timeout: 30, is_parallel: false)
test('m7-legacy-binary', python, args: [meson.project_source_root() / 'tests/m7_legacy.py', binary_fixture],
  workdir: meson.project_source_root() / 'tests', timeout: 30, is_parallel: false)
test('m7-native-compatibility', python, args: [meson.project_source_root() / 'tests/m7_legacy.py',
  binary_fixture, 'observer-binary-m7.json'], workdir: meson.project_source_root() / 'tests',
  depends: binary_fixture, timeout: 30, is_parallel: false)
test('m7-native-reference', python, args: [meson.project_source_root() / 'tests/native_binary.py', binary_fixture,
  'observer-binary-m7.json'], workdir: meson.project_source_root() / 'tests', timeout: 30, is_parallel: false)
test('native-binary-decoder', python, args: ['-m', 'unittest', 'tools.g2.tests.test_native_binary'],
  workdir: meson.project_source_root(), timeout: 30)

test('m6-smoke', python, args: [meson.project_source_root() / 'tests/m6_smoke.py', dpds_exe],
  workdir: meson.project_source_root(), timeout: 30, is_parallel: false)

binary_io = executable('observer-binary-io-test', ['tests/observer_binary_io.c', 'common/observer_binary.c'],
  dependencies: [dpdk, threads], c_args: ['-DNDEBUG'])
test('observer-binary-io', python, args: [meson.project_source_root() / 'tests/eal_args.py',
  binary_io.full_path(), '--no-pci', '--no-huge', '--no-telemetry', '--in-memory', '--file-prefix=hng-binary-io'],
  depends: binary_io, timeout: 30, is_parallel: false)

test('observer-shutdown', python, args: [meson.project_source_root() / 'tests/observer_shutdown.py', dpds_exe],
  timeout: 30, is_parallel: false)

test('g2-sampled', python, args: ['-m','unittest','tools.g2.tests.test_sampled_reference',
  'tools.g2.tests.test_sampled_bundle','tools.g2.tests.test_seqcheck',
  'tools.g2.tests.test_native_arrays','tools.g2.tests.test_array_sampling',
  'tools.g2.tests.test_pcapng_arrays','tools.g2.tests.test_native_projection',
  'tools.g2.tests.test_sampled_binary','tools.g2.tests.test_sampled_columns',
  'tools.g2.tests.test_sample_reuse','tools.g2.tests.test_exact_q'],
  workdir: meson.project_source_root(), timeout: 60, is_parallel: false)

g2_matrix_environment = environment()
g2_matrix_environment.prepend('PATH', meson.project_build_root())
test('g2-matrix', python, args: ['-m','unittest','tools.g2.tests.test_matrix',
  'tools.g2.tests.test_matrix_seqcheck','tools.g2.tests.test_deployment',
  'tools.g2.tests.test_x710_matrix','tools.g2.tests.test_matrix_preflight',
  'tools.g2.tests.test_m6_x710_full','tools.g2.tests.test_judge_shards','tools.g2.tests.test_bprime_inputs'], env: g2_matrix_environment, depends: seqcheck,
  workdir: meson.project_source_root(), timeout: 60, is_parallel: false)

test('g2-raw-retention', python, args: ['-m','unittest',
  'tools.g2.tests.test_raw_retention_budget','tools.g2.tests.test_raw_prefix','tools.g2.tests.test_raw_prune'],
  env: g2_matrix_environment, depends: seqcheck,
  workdir: meson.project_source_root(), timeout: 60, is_parallel: false)

test('g2-prefix-inspection', python, args: ['-m','unittest','tools.g2.tests.test_prefix_report.PrefixReportTests'],
  env: g2_matrix_environment, depends: seqcheck,
  workdir: meson.project_source_root(), timeout: 60, is_parallel: false)

test('g2-prefix-validation', python, args: ['-m','unittest','tools.g2.tests.test_prefix_report.PrefixValidationTests'],
  env: g2_matrix_environment, depends: seqcheck,
  workdir: meson.project_source_root(), timeout: 60, is_parallel: false)

test('g2-fullrate-observer', python, args: ['-m','unittest','tools.g2.tests.test_fullrate_observer','tools.g2.tests.test_fullrate_plan'],
  depends: seqcheck, workdir: meson.project_source_root(), timeout: 60, is_parallel: false)

test('g2-m5', python, args: ['-m','unittest','tools.g2.tests.test_m5_qualify'],
  env: g2_matrix_environment, depends: seqcheck,
  workdir: meson.project_source_root(), timeout: 60, is_parallel: false)
physical_fixture = executable('observer-physical-fixture',
  ['dpds.c', 'core/receive.c', 'core/combined.c', 'tests/observer_physical_pmd.c'] + common_sources,
  dependencies: [dpdk, core, threads], c_args: ['-UNDEBUG'],
  link_args: ['-Wl,--wrap=rte_eth_dev_info_get', '-Wl,--wrap=rte_dev_bus', '-Wl,--wrap=rte_bus_name'],
  override_options: ['b_lto=false'], build_rpath: meson.project_source_root() / 'dpds-core/target/release')
test('observer-physical', python, args: [meson.project_source_root() / 'tests/observer_physical.py', dpds_exe, physical_fixture],
  timeout: 30, is_parallel: false)
