set -euo pipefail

base=81652df2aaefcfb3ec9fbc96c752689babd2031e
candidate=758caa98f56bdd8f3e48fabc88fa4d7e8a8fcd96
kit="$PWD/RWXblast-radius-sample"
audit="$PWD/.github/validation"
temp="$(mktemp -d)"
trap 'rm -rf "$temp"' EXIT
chmod 755 "$temp"

for commit in "$base" "$candidate"; do
    dest="$temp/$commit"
    mkdir "$dest"
    git archive "$commit" | tar -x -C "$dest"
    /usr/bin/time -p timeout 600 docker build --platform linux/amd64 -t "rwx:$commit" "$dest"
done

docker run --rm --platform linux/amd64 --network none --read-only --user 65534:65534 \
    --entrypoint python "rwx:$candidate" -c '
import json
from pathlib import Path
data = json.loads(Path("/sqlglot/.rwx-executor-cases.json").read_text())
print("Profiled TPC-DS queries:", len(data["ms"]))
assert len(data["ms"]) == 99
'

read -r -d '' runner <<'PY' || true
import subprocess
import sys
from pathlib import Path

import resource

result = subprocess.run([sys.executable, "/app/rwx/selector.py"])
usec = None
try:
    for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines():
        key, value = line.split()
        if key == "usage_usec":
            usec = int(value)
            break
except OSError:
    pass
if usec is None:
    # cgroup v2 file missing (GitHub runners): the selector and every process it
    # waited on are this process's children.
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    usec = int((r.ru_utime + r.ru_stime) * 1_000_000)
print("\nCPU_USEC", usec, file=sys.stderr)
sys.exit(result.returncode)
PY

summary="$temp/summary.tsv"
for batch in sample targeted; do
    if [[ "$batch" == sample ]]; then
        data="$kit/sample"
        tests="$data/tests.json"
    else
        data="$temp/targeted"
        mkdir -p "$data"
        cp -R "$audit/commits" "$audit/answers.json" "$kit/sample/tests.json" "$data/"
        tests="$data/tests.json"
    fi
    for run in 1 2 3; do
        pair=("$base" "$candidate")
        if [[ "$run" == 2 ]]; then pair=("$candidate" "$base"); fi
        for commit in "${pair[@]}"; do
            label=baseline
            if [[ "$commit" == "$candidate" ]]; then label=candidate; fi
            prefix="$temp/$batch-$label-$run"
            name="rwx-$batch-$label-$run"
            if ! timeout 180 docker run --rm --name "$name" --platform linux/amd64 \
                --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 \
                --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
                --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
                --volume "$data/commits:/data/commits:ro" --volume "$tests:/data/tests.json:ro" \
                --entrypoint python "rwx:$commit" -c "$runner" > "$prefix.jsonl" 2> "$prefix.log"; then
                tail -80 "$prefix.log"
                docker rm -f "$name" || true
                exit 1
            fi
            cpu="$(awk 'match($0, /CPU_USEC [0-9]+/) {split(substr($0, RSTART, RLENGTH), a, " "); printf "%.6f", a[2] / 1000000}' "$prefix.log")"
            printf '\n%s %s run %s\n' "$batch" "$label" "$run"
            python3 "$kit/score.py" "$data" "$data/answers.json" "$prefix.jsonl" --cpu-seconds "$cpu" | tee "$prefix.score"
            grep -E 'checked affected TPC-DS' "$prefix.log" || true
            awk -v batch="$batch" -v label="$label" '/^Total:/ {print batch, label, $2}' "$prefix.score" >> "$summary"
            if ! grep -qE '^Misses: +0 ' "$prefix.score"; then
                exit 1
            fi
        done
    done
    for label in baseline candidate; do
        cmp "$temp/$batch-$label-1.jsonl" "$temp/$batch-$label-2.jsonl"
        cmp "$temp/$batch-$label-1.jsonl" "$temp/$batch-$label-3.jsonl"
    done
done

python3 - "$summary" <<'PY'
import statistics
import sys
from collections import defaultdict
from pathlib import Path

values = defaultdict(list)
for line in Path(sys.argv[1]).read_text().splitlines():
    batch, label, seconds = line.split()
    values[batch, label].append(float(seconds))
for (batch, label), seconds in values.items():
    print(f"MEDIAN {batch} {label} {statistics.median(seconds):.6f} s")
PY
