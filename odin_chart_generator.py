import random
import copy

class ChartGenerator:
    def __init__(self, chart_base=256, mask_base=1000000, num_digits=8, point_of_reference=1):
        self.chart_base = chart_base
        self.mask_base = mask_base
        self.num_digits = num_digits
        self.point_of_reference = point_of_reference
        
        self.P = [point_of_reference] + [0] * (num_digits - 1)
        self.L = [1] + [0] * (num_digits - 1)
        self.N = [1] + [0] * (num_digits - 1)   # Single N stream for alpha
        self.R = [2] + [0] * (num_digits - 1)
        self.D = [1] + [0] * (num_digits - 1)
        
        self._update_derived()

    def _update_derived(self):
        distance = self.L[0] - self.P[0]
        self.R[0] = 2 + distance * self.chart_base
        self.D[0] = 1 + distance * (self.chart_base - 1)

    def encode_byte(self, byte):
        v = self.L[0]
        r = self.R[0]
        new_v = v + (((v - r) * (self.chart_base - 1)) + byte)
        
        self.L[0] = new_v
        self.N[0] = new_v
        self._update_derived()
        return byte

    def decode_byte(self):
        v = self.N[0]
        r = self.R[0]
        
        prev_v = (v // self.chart_base) + 1
        grok = prev_v + (((prev_v - r) * (self.chart_base - 1)) + 1)
        
        diff = v - grok + 1
        recovered = diff % self.chart_base
        
        self.L[0] = prev_v
        self._update_derived()
        return recovered

    def print_state(self):
        print(f"L={self.L[0]}  R={self.R[0]}  D={self.D[0]}  N={self.N[0]}")
