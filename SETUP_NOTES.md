# eAudit setup notes

## 1. Install

### 1a. Build eAudit itself

```bash
cd /path/to/eaudit
make all
```

Produces `eaudit` (parser) and `ecapd.so` (capture shared object loaded by `eauditd.py`).

### 1b. Install BCC from source (required)

The Ubuntu package `python3-bpfcc` is too old — it lacks `atomic_increment` on BPF
tables, which `eauditk.c` uses. Remove it if present and build BCC from `iovisor/bcc`.
Use the `bcc_install_ubuntu22.sh` or `bcc_install_ubuntu24.sh` script as a reference
for your distro, or follow the steps below:

```bash
# clean out distro BCC if it was installed
sudo apt purge -y python3-bpfcc bpfcc-tools libbpfcc

# build deps (Ubuntu 22 example — see bcc_install_*.sh for other distros)
sudo apt install -y zip bison build-essential cmake flex git libedit-dev \
    libllvm14 llvm-14-dev libclang-14-dev python3 zlib1g-dev libelf-dev \
    libfl-dev python3-setuptools liblzma-dev libdebuginfod-dev

# fetch + build
mkdir -p src && cd src
git clone --depth 1 https://github.com/iovisor/bcc.git
mkdir bcc/build && cd bcc/build
cmake ..
make -j$(nproc)
sudo make install
cmake -DPYTHON_CMD=python3 ..
pushd src/python/
make && sudo make install
popd
sudo ldconfig
```

Verify:

```bash
python3 -c "import bcc; print(bcc.__file__)"
# -> /usr/lib/python3/dist-packages/bcc-0.0.0+<hash>-py3.10.egg/bcc/__init__.py
```

## 2. Run capture (`ecapd`)

`ecapd` is a wrapper that runs `sudo python3 eauditd.py`. Needs root for eBPF.

Foreground (Ctrl-C to stop):

```bash
sudo ./ecapd -c /path/to/capture.cap
```

Background:

```bash
sudo ./ecapd -c /tmp/eaudit.cap -v3 > /tmp/ecapd.log 2>&1 &
```

Useful flags (`./ecapd -h` for all):

- `-c <file>`     capture file output (binary)
- `-b <KB>`       per-CPU buffer size (0.01 .. 8)
- `-r <MB>`       ring buffer size (powers of 2, 1..64)
- `-v<0-3>`       verbosity
- `-s`            print syscall summary on exit
- `-u[mor]`       include unsuccessful mprotect / open / read-write
- `-S`            disable 16-bit sequence numbers

### 2a. Timed capture (`run_timed.py`)

For unattended captures, `run_timed.py` wraps `ecapd` and stops it cleanly
after a duration or at a wall-clock time. It auto-re-execs under `sudo` if
needed, forwards SIGINT correctly, and waits out BCC's (slow) kprobe
teardown without losing the capture.

**Basic usage:**

```bash
# capture for 10 seconds
sudo ./run_timed.py -d 10s -o data/test1/data.cap

# capture for 30 minutes
sudo ./run_timed.py -d 30m -o /tmp/eaudit.cap

# capture for 1 hour 30 min
sudo ./run_timed.py -d 1h30m -o captures/run1.cap

# stop at an absolute wall-clock time (UTC unless zone given)
sudo ./run_timed.py -u '2026-04-19 22:00 UTC' -o /tmp/eaudit.cap
sudo ./run_timed.py -u '2026-04-19:22:00 UTC' -o /tmp/eaudit.cap
sudo ./run_timed.py -u '2026-04-19T22:00:00+00:00' -o /tmp/eaudit.cap
```

**All flags:**

| flag | meaning |
|---|---|
| `-d <dur>`  | duration: `30m`, `1h`, `1h30m`, `1d`, `90s`, or bare seconds |
| `-u <when>` | absolute stop time (UTC default). Mutually exclusive with `-d`. |
| `-o <path>` | capture file path (passed to `ecapd` as `-c`). Required. |
| `--ecapd <path>` | override the `ecapd` binary location (default: alongside the script) |
| `-g <seconds>` | how long to wait for BCC teardown after SIGINT (default 60) |
| `--` | **passthrough separator** — everything after it is forwarded verbatim to `ecapd` |

**Typical output (10s run):**

```
[run_timed] starting: /path/to/eaudit/ecapd -c data/test1/data.cap
[run_timed] will stop at 2026-04-16T18:13:27 (in 10s)
Logprinter: 0M records, average size 8
[run_timed] time elapsed, sending SIGINT
Pid 37806: Read 16422B in 187 msgs, avg msglen 87, wrote 16431B
16.42KiB (20 lost), Size: record=87
[run_timed] waiting for ecapd to finish teardown (5/60s — capture is already safely written)
[run_timed] wrote 16431 bytes to data/test1/data.cap (ecapd rc=0)
```

The teardown-progress ticks are normal: BCC's kprobe detach takes
10–30s on some kernels *after* the capture file has already been safely
written. Exit code is `0` as long as the capture file has bytes.

### 2b. Passing ecapd tuning flags through `run_timed.py`

Everything after `--` is forwarded to `ecapd` unchanged:

```bash
sudo ./run_timed.py -d 10s -o data/test1/data.cap -- -r 16
sudo ./run_timed.py -d 10s -o data/test1/data.cap -- -b 4 -r 32
```

The two tuning flags you will most often touch:

| flag | what it does | default | when to change |
|---|---|---|---|
| `-b <KB>` | per-CPU BPF buffer size, 0.01–8 KB | small | bump when `-r` alone isn't enough |
| `-r <MB>` | shared ring buffer size, power of 2 in 1..64 MB | small | bump first when you see drops |

Both control how much in-kernel memory eBPF has to hold events before
`eauditd.py` drains them to disk. Bigger buffers = fewer lost events
during bursts, at the cost of RAM. You do **not** need to set these for
most captures.

**When to add them:** only if you see `(N lost)` with `N > 0` in the
shutdown summary. That line means the ring buffer overran during a burst
and the kernel dropped events. Bump `-r` first; only add `-b` if losses
persist.

Rule of thumb by workload:

| workload | suggested flags |
|---|---|
| idle / light desktop, short captures | *(none — defaults are fine)* |
| busy dev box, hour-long captures | `-- -r 16` |
| heavy server under load / dataset collection | `-- -b 4 -r 32`  or  `-- -b 4 -r 64` |

## 3. Stop capture

Send **SIGINT** — `eauditd.py` catches it and flushes the ring buffer before exit. Never `kill -9`; that loses buffered events.

```bash
sudo pkill -INT -f eauditd.py
# or:
sudo kill -INT <pid>
# or Ctrl-C if foreground
```

On clean shutdown you will see:

```
Received interrupt, emptying ring buffer
Pid <n>: Read <bytes>B in <msgs> msgs, avg msglen ...
<KiB> (0 lost), Size: record=...
```

`0 lost` means no ring-buffer overruns.

## 4. Where the logs are

- **Raw capture**: wherever you passed `-c`. Binary format, owned by root (permissions `640`). `chmod a+r` it if you want to parse as non-root.
- **Parser stdout**: goes wherever you redirect `eaudit`'s `-P` (or stdout).
- **ecapd runtime log** (stats, errors): wherever you redirected `ecapd`.

## 5. Parse captures (`eaudit`)

`eaudit` runs as a normal user. Reads from `-I <file>` or stdin.

```bash
# Human-readable dump
./eaudit -I /tmp/eaudit.cap -P /tmp/eaudit.out

# List files accessed and sockets used
./eaudit -I /tmp/eaudit.cap -pf -ps

# Sorted syscall frequency counts
./eaudit -I /tmp/eaudit.cap -c

# Record opens + reads/writes into a gzip record file
./eaudit -I /tmp/eaudit.cap -o -r -R /tmp/records.gz
```

Useful flags (`./eaudit -h`):

- `-I <file>`   input capture file (else stdin)
- `-P [<file>]` print human-readable output (default stdout)
- `-R <file>`   record file (gzip if `.gz`)
- `-pf` / `-ps` print file list / socket list
- `-o` / `-r`   record opens / reads+writes into `-R` file
- `-c`          sort counts by frequency
- `-s`          capture has NO sequence numbers (match `ecapd -S`)
- `-i <ip>`     your IP (required for correctly classifying remote vs local net)
- `-n <net/mask>` local network(s)

## 6. Sanity check output

If you see `execve` with argv/env, `open` with file paths, and socket/connect entries,
parsing is working:

```
pid=11400: execve(file="/bin/bash", argv=...)
pid=11402: open(file="/etc/passwd", flags=80000, mode=0) ret=6
pid=11403: connect(fd=7, endpoint=IP4:93.184.216.34:443) ret=0
```

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'bcc'` | BCC not installed / installed for wrong Python | rebuild bindings step in 1b |
| `no member named 'atomic_increment'` | Distro BCC is too old | Uninstall `python3-bpfcc`, build BCC from source |
| `ecapd` exits immediately, capture file empty | Not root, or BPF load failure | Run via `sudo`; check ecapd log for the real error |
| `<KiB> (N lost)` with N>0 | Ring buffer overrun | Raise `-r` (ring buffer) / `-b` (per-CPU buffer) |
| `python3: can't open file '.../eauditd.py'` | Called ecapd from wrong directory | Fixed in this repo; pull latest `ecapd` |
