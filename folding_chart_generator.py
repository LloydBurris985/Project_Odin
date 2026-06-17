
import math, json
from chart_generator import ChartGenerator, HandMath, fmt_short, fmt_large

class FoldTriangle:
    __slots__ = ("step","leg_a","leg_b","hyp","R_before","R_after","V_at_fold")
    def __init__(self,step,leg_a,leg_b,R_before,R_after,V_at_fold):
        self.step=step; self.leg_a=leg_a; self.leg_b=leg_b
        self.hyp=math.isqrt(leg_a*leg_a+leg_b*leg_b)
        self.R_before=R_before; self.R_after=R_after; self.V_at_fold=V_at_fold
    def to_dict(self):
        return {"step":self.step,"leg_a":str(self.leg_a),"leg_b":str(self.leg_b),"hyp":str(self.hyp),"R_before":str(self.R_before),"R_after":str(self.R_after),"V_at_fold":str(self.V_at_fold)}
    @staticmethod
    def from_dict(d):
        ft=FoldTriangle.__new__(FoldTriangle)
        ft.step=int(d["step"]); ft.leg_a=int(d["leg_a"]); ft.leg_b=int(d["leg_b"])
        ft.hyp=int(d["hyp"]); ft.R_before=int(d["R_before"]); ft.R_after=int(d["R_after"]); ft.V_at_fold=int(d["V_at_fold"])
        return ft

class FoldingChartGenerator(ChartGenerator):
    def __init__(self,chart_base=256,mask_base=1_000_000_000_000,num_digits=100,num_n_streams=12,fold_threshold=1000,auto_fold_every=None):
        super().__init__(chart_base,mask_base,num_digits,num_n_streams)
        self._fold_threshold=fold_threshold; self._auto_fold_every=auto_fold_every
        self._triangle_log=[]; self._decode_fold_ptr=0
    def _maybe_fold(self,byte_val,u=0):
        if self._fold_threshold < 0: return False   # sentinel: rolling=OFF, never fold
        hm=self.hm; V_int=hm.to_int(self.Vs[u]); R_int=hm.to_int(self.Rs[u])
        if R_int==0: return False
        drift=abs(V_int-R_int)*1000//R_int
        if drift<=self._fold_threshold: return False
        leg_a=abs(V_int-R_int); R_after=V_int
        # step_count was already incremented by _encode_step, so fold fired after step (step_count-1)
        fold_step = self.step_count - 1
        tri=FoldTriangle(step=fold_step,leg_a=leg_a,leg_b=byte_val,R_before=R_int,R_after=R_after,V_at_fold=V_int)
        self._triangle_log.append(tri); self.Rs[u]=hm.from_int(R_after)
        self.fold_stats[u].record_fold(fold_step,V_int,R_int,R_after)
        return True
    def _encode_step_folded(self,byte_val,u=0):
        self._encode_step(byte_val,u); self._maybe_fold(byte_val,u)
        if self._auto_fold_every and self.step_count>0 and self.step_count%self._auto_fold_every==0:
            self._maybe_fold(byte_val,u)
    def _decode_step_folded(self,u=0):
        # Decode unrolls encode in reverse: decode step k undoes encode step (N-1-k).
        # We walk the fold log backward: _decode_fold_ptr starts at len(log)-1.
        # When the encode step being unrolled < the fold step, the fold had NOT fired yet,
        # so we must restore R_before for that fold.
        while (self._decode_fold_ptr >= 0 and
               self._triangle_log[self._decode_fold_ptr].step >= (self._total_encode_steps - self.step_count - 1)):
            tri = self._triangle_log[self._decode_fold_ptr]
            self.Rs[u] = self.hm.from_int(tri.R_before)
            self._decode_fold_ptr -= 1
        byte_val = self._decode_step(u)
        return byte_val
    def encode_bytes(self,data,u=0):
        hm=self.hm; self.Vs[u]=hm.from_int(1); self.Rs[u]=hm.from_int(1); self.step_count=0
        self._triangle_log.clear(); self._decode_fold_ptr=0
        for i in range(len(data)-1,-1,-1): self._encode_step_folded(data[i],u)
        return hm.to_int(self.Vs[u])
    def decode_bytes(self,V,length,log=None,u=0,r_start=None):
        hm=self.hm
        needed=0; v_tmp=V
        while v_tmp>0: needed+=1; v_tmp//=hm.M
        if needed>hm.D: hm.D=needed+8
        self.Vs[u]=hm.from_int(V); self.step_count=0
        self._triangle_log=[FoldTriangle.from_dict(d) for d in log] if log else []
        self._total_encode_steps = length
        # Start fold pointer at last fold (walk backward during decode)
        self._decode_fold_ptr = len(self._triangle_log) - 1
        # Initialise R: if caller supplies r_start use it; else use R_after of last fold;
        # else R=1 (no folds case).
        if r_start is not None:
            self.Rs[u] = hm.from_int(r_start)
        elif self._triangle_log:
            self.Rs[u] = hm.from_int(self._triangle_log[-1].R_after)
        else:
            self.Rs[u] = hm.from_int(1)
        return bytes(self._decode_step_folded(u) for _ in range(length))
    def export_fold_log(self): return [t.to_dict() for t in self._triangle_log]
    def import_fold_log(self,data): self._triangle_log=[FoldTriangle.from_dict(d) for d in data]; self._decode_fold_ptr=0
    def print_fold_log(self,max_rows=20): print(f"  Fold log: {len(self._triangle_log)} events")
    def fold_summary(self,universe=0):
        hm=self.hm; V_int=hm.to_int(self.Vs[universe]); R_int=hm.to_int(self.Rs[universe])
        return {"fold_count":len(self._triangle_log),"fold_threshold":self._fold_threshold,"step_count":self.step_count,"V":V_int,"R":R_int,"drift_x1000":abs(V_int-R_int)*1000//R_int if R_int else 0,"triangles":[t.to_dict() for t in self._triangle_log[-5:]]}
