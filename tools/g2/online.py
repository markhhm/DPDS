# SPDX-License-Identifier: Apache-2.0
"""Bounded online cell statistics and sampled retention, independent of the DUT.

The capture/join caller supplies verified identities and calibrated OWD pairs.
This module does not certify those upstream facts or hardware qualification.
"""
from collections import Counter
import hashlib

from .reference import canonical, uint
from .report import timing_gate, timing_stats


def sample(row, seed, mask_bits=10):
    uint(seed,"sampling seed")
    uint(mask_bits,"sampling mask",10)
    key=[row["run"],row["ingress_port"],row["external_identity"]]
    word=int.from_bytes(hashlib.sha256(b"hng-g2-timing-v1\0"+seed.to_bytes(8,"big")+canonical(key)).digest()[:8],"big")
    return word & ((1<<mask_bits)-1)==0


class Histogram:
    def __init__(self):
        self.n=0
        self.minimum=self.maximum=None
        self.bins=Counter()
        self.underflow=self.overflow=0
        self.nonzero=0

    def add(self,value):
        self.n+=1
        self.nonzero+=value!=0
        self.minimum=value if self.minimum is None else min(self.minimum,value)
        self.maximum=value if self.maximum is None else max(self.maximum,value)
        if value < -10_000_000:
            self.underflow+=1
        elif value > 10_000_000:
            self.overflow+=1
        else:
            self.bins[value//1000]+=1

    def snapshot(self):
        return dict(n=self.n,min_ns=self.minimum,max_ns=self.maximum,bin_width_ns=1000,
                    bins={str(key):value for key,value in sorted(self.bins.items())},
                    underflow=self.underflow,overflow=self.overflow,nonzero=self.nonzero)


class Retention:
    """Select persisted witnesses; full events continue through the online oracle."""
    def __init__(self,manifest):
        self.registration=manifest
        if manifest["scope"] not in {"software-only","physical"}:
            raise ValueError("unsupported measurement scope")
        policy=manifest.get("retention",dict(mode="stream",debug_windows=[],mismatch_cap=1_000_000))
        self.mode=policy["mode"]
        if self.mode not in {"stream","full"}:
            raise ValueError("unsupported retention mode")
        self.windows=policy["debug_windows"]
        self.cap=uint(policy["mismatch_cap"],"mismatch cap")
        keys=set()
        for window in self.windows:
            start=uint(window["start_ns"],"debug start")
            end=uint(window["end_ns"],"debug end")
            if end<=start or end-start>2_000_000_000:
                raise ValueError("debug window must be positive and <=2 s")
            key=(window.get("direction"),window.get("line"),window["phase"])
            if key in keys:
                raise ValueError("at most one debug window per cell")
            keys.add(key)
        if manifest["scope"]=="physical" and self.mode=="full":
            if any(window["end_ns"]-window["start_ns"]>2_000_000_000 for window in manifest["windows"]):
                raise ValueError("physical full retention exceeds debug cap")
        self.mismatch_count=0
        self.issues=[]

    def keep(self,row,mismatch=False):
        if mismatch:
            self.mismatch_count+=1
            if self.mismatch_count<=self.cap:
                return True
            if "mismatch_cap_exhausted" not in self.issues:
                self.issues.append("mismatch_cap_exhausted")
        if self.mode=="full":
            return True
        if row.get("external_identity") is not None:
            mask=self.registration["sampling"].get("mask_bits",10)
            phases={window["phase"] for window in self.registration["windows"]
                    if window["start_ns"]<=row["arrival_ns"]<window["end_ns"]}
            for cell in self.registration["required_cells"]:
                if (cell["direction"]==str(row["ingress_port"]) and cell["line"]==row["line"]
                        and cell["phase"] in phases):
                    mask=cell.get("mask_bits",mask)
                    break
            if sample(row,self.registration["sampling"]["seed"],mask):
                return True
        return any(window["start_ns"]<=row["arrival_ns"]<window["end_ns"]
                   and window.get("line",row.get("line"))==row.get("line")
                   and str(window.get("direction",row["ingress_port"]))==str(row["ingress_port"])
                   for window in self.windows)


class ReceiveOrder:
    """Reconstruct contiguous wire order from bounded, possibly reordered exports."""
    def __init__(self,capacity):
        self.capacity=uint(capacity,"receiver reorder capacity")
        self.next={}
        self.pending={}

    def push(self,row):
        port=uint(row["egress_port"],"egress",65535)
        seq=uint(row["receive_seq"],"receive sequence")
        prefix=self.next.get(port,0)
        if seq<prefix or (port,seq) in self.pending:
            raise ValueError("duplicate receive sequence")
        if len(self.pending)>=self.capacity and seq!=prefix:
            raise ValueError("receive ordering capacity exhausted")
        self.pending[port,seq]=row
        ready=[]
        while (port,prefix) in self.pending:
            ready.append(self.pending.pop((port,prefix)))
            prefix+=1
        self.next[port]=prefix
        return ready

    def finish(self):
        return ["receive_sequence_gap"] if self.pending else []


class OnlineCells:
    """Consume every model decision and verified delivery; retain only main errors."""
    def __init__(self,manifest):
        self.manifest=manifest
        self.cells={}
        self.retention=Retention(manifest)
        self.sample_capacity=uint(manifest["capacity"]["timing_sample_capacity"],"timing sample capacity")
        if not self.sample_capacity:
            raise ValueError("timing sample capacity must be positive")
        for cell in manifest["required_cells"]:
            key=(str(cell["direction"]),cell["line"],cell["phase"])
            if key in self.cells:
                raise ValueError("duplicate required cell")
            self.cells[key]=dict(registration=cell,counts=Counter(),groups={},errors=[],uncertainty=0,
                                 populations=Counter(),lost_groups=Counter(),failures=Counter(),incomplete=set(),q_peak=0,q_drain=0)

    def _cell(self,row):
        if row["run"]!=self.manifest["run"]:
            raise ValueError("mixed run identifiers")
        phases=[window["phase"] for window in self.manifest["windows"]
                if window["start_ns"]<=row["arrival_ns"]<window["end_ns"]]
        if len(phases)!=1:
            raise ValueError("ingress belongs to missing/overlapping windows")
        key=(str(row["ingress_port"]),row["line"],phases[0])
        return self.cells.get(key)

    def _main(self,row,cell):
        mask=cell["registration"].get("mask_bits",self.manifest["sampling"].get("mask_bits",10))
        return sample(row,self.manifest["sampling"]["seed"],mask)

    def _strata(self,row,cell):
        if self._main(row,cell):
            return ["main","late-main" if row["late"] else "ordinary-main"]
        return (["late_extra"] if row["late"] else [])+(
            ["app_extra:"+row["app"]] if row.get("app") in self.manifest["sampling"]["oversample_apps"] else [])

    def expect(self,row):
        cell=self._cell(row)
        if cell is None:
            return
        counts=cell["counts"]
        counts["ingress"]+=1
        counts[row["outcome"]]+=1
        cell["q_peak"]=max(cell["q_peak"],row["q_bytes"])
        cell["q_drain"]=max(cell["q_drain"],row["q_drain_ns"])
        if row.get("selected_late") is not None:
            counts["probe_sent"]+=1
        if row["outcome"]=="accepted" and self._main(row,cell):
            counts["main_selected"]+=1
        if row["outcome"]=="accepted":
            cell["populations"].update(self._strata(row,cell))

    def delivery(self,row,owd_ns,uncertainty_ns,expected_ns=None,raw_owd_ns=None):
        cell=self._cell(row)
        if cell is None:
            return
        if row["outcome"]!="accepted":
            cell["failures"]["delivered_intended_drop"]+=1
            cell["counts"]["unexpected_delivery"]+=1
            return
        cell["counts"]["received"]+=1
        if row.get("selected_late") is not None:
            cell["counts"]["probe_received"]+=1
        names=self._strata(row,cell)
        if owd_ns is None:
            return
        uint(uncertainty_ns,"pair uncertainty")
        if type(owd_ns) is not int:
            raise ValueError("OWD must be integer ns")
        if owd_ns < -uncertainty_ns:
            cell["incomplete"].add("invalid_clock_pair")
        if self.manifest["scope"]=="physical" and uncertainty_ns>10000:
            cell["incomplete"].add("clock_uncertainty")
        if self.manifest.get("timing_mode","stress")=="calibration" and expected_ns is None:
            raise ValueError("calibration requires preregistered t + I*delta")
        target=row["release_ns"]-row["arrival_ns"] if expected_ns is None else expected_ns
        error=owd_ns-target
        cell["uncertainty"]=max(cell["uncertainty"],uncertainty_ns)
        main=self._main(row,cell)
        for name in names:
            group=cell["groups"].setdefault(name,(Histogram(),Histogram(),Histogram(),Histogram()))
            group[0].add(error)
            group[1].add(abs(error))
            group[2].add(owd_ns if raw_owd_ns is None else raw_owd_ns)
            group[3].add(owd_ns)
        if main:
            if len(cell["errors"])>=self.sample_capacity:
                cell["incomplete"].add("timing_sample_capacity")
            else:
                cell["errors"].append(abs(error))

    def loss(self,row,reason,complete):
        cell=self._cell(row)
        if cell is None:
            return
        cell["counts"]["datapath_lost" if complete else "unresolved"]+=1
        if complete:
            cell["failures"][reason]+=1
            cell["lost_groups"].update(self._strata(row,cell))
            if self._main(row,cell):
                cell["counts"]["main_datapath_lost"]+=1
        else:
            cell["incomplete"].add("observer_incomplete")

    def violation(self,row,code,measurement_status="FAIL"):
        cell=self._cell(row)
        if cell is not None:
            if measurement_status=="FAIL":
                cell["failures"][code]+=1
            else:
                cell["incomplete"].add(code)

    def finish(self,identity_index=None):
        if identity_index is not None:
            if (identity_index.get("schema")!="hng-g2-identity-index-v1"
                    or identity_index.get("run")!=self.manifest["run"]
                    or identity_index.get("released") is not True):
                raise ValueError("identity index report has mixed run/schema or is still active")
            seen=set()
            for row in identity_index["cells"]:
                key=row["direction"],row["line"],row["phase"]
                if key not in self.cells or key in seen:
                    raise ValueError("identity index has unregistered/duplicate cell")
                seen.add(key)
                count=uint(row["join_overflow"],"join overflow")
                self.cells[key]["counts"]["join_overflow"]=count
                if count:
                    self.cells[key]["incomplete"].add("join_overflow")
            if seen!=set(self.cells):
                raise ValueError("identity index report is missing registered cells")
            if sum(self.cells[key]["counts"]["join_overflow"] for key in seen)<uint(identity_index["join_overflow"],"join overflow"):
                raise ValueError("identity index overflow has no affected cell")
        rows=[]
        for (direction,line,phase),cell in self.cells.items():
            counts=cell["counts"]
            if counts["accepted"]>counts["received"]+counts["datapath_lost"]+counts["unresolved"]:
                cell["incomplete"].add("unresolved_delivery")
            if counts["received"]+counts["datapath_lost"]>counts["accepted"]:
                cell["failures"]["duplicate_or_unexpected_delivery"]+=1
            stats=timing_stats(cell["errors"])
            gates={name:timing_gate(stats,name,limit,cell["uncertainty"])
                   for name,limit in (("median",100000),("p99",500000))}
            if not counts["accepted"]-counts["datapath_lost"]:
                for gate in gates.values():gate["status"]="not_applicable"
            elif stats["n"]<368:
                cell["incomplete"].add("insufficient_main_samples")
            if stats["n"]+counts["main_datapath_lost"]<counts["main_selected"]:
                cell["incomplete"].add("missing_main_pairs")
            for name,population in cell["populations"].items():
                paired=cell["groups"][name][0].n if name in cell["groups"] else 0
                if population>paired+cell["lost_groups"][name]:
                    cell["incomplete"].add("missing_main_pairs" if name.endswith("main") else "missing_extra_pairs")
            gates["order"]=dict(status="FAIL" if any("order" in name for name in cell["failures"]) else "PASS",threshold=0)
            gates["storage"]=dict(status="FAIL" if cell["failures"]["delay_storage_drop"] else "PASS",threshold=0,
                                  value=cell["failures"]["delay_storage_drop"])
            failures=list(cell["failures"])+[name for name,gate in gates.items() if gate["status"]=="FAIL"]
            incomplete=sorted(cell["incomplete"])
            state="INCONCLUSIVE" if incomplete else "FAIL" if failures else "INCONCLUSIVE" if any(gate["status"]=="INCONCLUSIVE" for gate in gates.values()) else "PASS"
            groups={name:dict(n=signed.n,signed_error=signed.snapshot(),absolute_error=absolute.snapshot(),
                             raw_owd=raw.snapshot(),corrected_owd=corrected.snapshot())
                    for name,(signed,absolute,raw,corrected) in cell["groups"].items()}
            for name,population in cell["populations"].items():
                group=groups.setdefault(name,dict(n=0))
                group.update(population=population,datapath_lost=cell["lost_groups"][name],
                             missing=population-group["n"]-cell["lost_groups"][name])
            groups.setdefault("main",dict(n=0))
            groups["main"].update(stats)
            rows.append(dict(condition_id=self.manifest["condition_id"],repeat=cell["registration"].get("repeat",0),
                direction=direction,line=line,phase=phase,required=True,status="INVALID" if state=="INCONCLUSIVE" else state,
                measurement_status=state,g2_qualified=False,gates=gates,counts={**{name:0 for name in
                    ("ingress","accepted","received","datapath_lost","probe_sent","probe_received")},**dict(counts)},timing=groups,
                reason_codes=incomplete,observed_failures=failures,
                q=dict(peak_l2_bytes=cell["q_peak"],peak_drain_ns_at_c=cell["q_drain"])))
        status="FAIL" if any(row["status"]=="FAIL" for row in rows) else "INVALID" if any(row["status"]!="PASS" for row in rows) else "PASS"
        return dict(status=status,rows=rows,scope=self.manifest["scope"],g2_qualified=False,identity_index=identity_index,
                    banner="software-only, not G2" if self.manifest["scope"]=="software-only" else "requires complete physical evidence")
