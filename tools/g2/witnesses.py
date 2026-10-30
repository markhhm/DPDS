# SPDX-License-Identifier: Apache-2.0
"""Persist selected witnesses; subset seals always describe the full transport."""
import hashlib
import json

from .reference import canonical


class Witnesses:
    def __init__(self, cells, directory, readers):
        self.policy = cells.retention
        self.directory = directory
        self.files = {}
        if directory is not None:
            for token, reader in readers.items():
                # Filenames come from logical roles, never user-controlled path identifiers.
                name = f"{reader.header['role']}-{len(self.files)}.jsonl"
                if sum(other.header["role"]==reader.header["role"] for other in readers.values())==1:
                    name = reader.header["role"]+".jsonl"
                stream = (directory/name).open("x", encoding="ascii", newline="\n")
                self.files[token] = dict(stream=stream, name=name, hash=hashlib.sha256(), count=0)
                self._write(token, reader.header)

    def _write(self, token, row):
        if token not in self.files:
            return
        item = self.files[token]
        data = canonical(row)+b"\n"
        item["stream"].write(data.decode("ascii"))
        item["hash"].update(data)
        item["count"] += row["kind"] not in {"header", "seal"}

    def keep(self, row, expected=None, active=None):
        if self.policy.mode=="full" or row["kind"] in {"frontier", "end", "summary", "counter"}:
            self._write(row["stream_id"], row)
        elif row["kind"] in {"dispatch", "attempt"}:
            field = "identities" if row["kind"]=="dispatch" else "offered"
            selected = []
            for item in row[field]:
                state = active.get((item["ingress_port"], item["external_identity"])) if active is not None else None
                if state is not None and self.policy.keep(state["expected"]):
                    selected.append(item)
            if selected:
                # This is a documented subset witness, not a replayable transport record.
                self._write(row["stream_id"], dict(row, **{field:selected}))
        elif expected is not None and self.policy.keep(expected):
            self._write(row["stream_id"], row)

    def finish(self, readers):
        for token, reader in readers.items():
            integrity = reader.integrity
            integrity["oracle_records_sha256"] = integrity.get("records_sha256")
            integrity["producer_records_sha256"] = reader.seal.get("records_sha256") if reader.seal else None
            if token in self.files:
                if reader.seal is not None:
                    self._write(token, reader.seal)
                item = self.files[token]
                item["stream"].close()
                integrity.update(retained_path=item["name"], retained_file_sha256=item["hash"].hexdigest(),
                                 retained_record_count=item["count"], retained_subset=self.policy.mode!="full")

    def online(self, result):
        if self.directory is not None:
            artifact = dict(schema="hng-g2-online-v1", run=result["run"], scope=result["scope"],
                status=result["status"], rows=result["rows"], stream_integrity=result["stream_integrity"],
                mismatches=result["mismatches"], identity_index=result["identity_index"],
                observer_cost=result["observer_cost"])
            (self.directory/"online.json").write_text(json.dumps(artifact, allow_nan=False, sort_keys=True)+"\n")
