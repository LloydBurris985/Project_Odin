import random

class ChartGenerator:
    def __init__(self, mask_base=1000000, chart_base=256, num_digits=12, num_n_streams=8):
        self.mask_base = mask_base
        self.chart_base = chart_base                    self.num_digits = num_digits
        self.num_n_streams = num_n_streams

        self.P = [1] + [0] * (num_digits - 1)
        self.L = [1] + [0] * (num_digits - 1)
        self.Ns = [[1] + [0] * (num_digits - 1) for _ in range(num_n_streams)]
        self.R = [2] + [0] * (num_digits - 1)           self.D = [1] + [0] * (num_digits - 1)
                                                        self._update_derived()

    def _update_derived(self):
        distance = self._subtract_lists(self.L, self.P)
        base_r = [2] + [0] * (self.num_digits - 1)
        self.R = self._add_lists(base_r, self._multiply_scalar(distance, self.chart_base))
        base_d = [1] + [0] * (self.num_digits - 1)
        self.D = self._add_lists(base_d, self._multiply_scalar(distance, self.chart_base - 1))

    def _add_lists(self, a, b):
        result = a[:]
        carry = 0
        for i in range(max(len(result), len(b))):
            if i == len(result):
                result.append(0)
            val = carry + (b[i] if i < len(b) else 0)
            temp = result[i] + val
            result[i] = temp % self.mask_base
            carry = temp // self.mask_base
        while carry:
            result.append(carry % self.mask_base)
            carry //= self.mask_base
        return result

    def _subtract_lists(self, a, b):
        result = a[:]
        borrow = 0
        for i in range(len(result)):
            val = b[i] if i < len(b) else 0
            temp = result[i] - val - borrow
            if temp < 0:
                temp += self.mask_base
                borrow = 1
            else:
                borrow = 0
            result[i] = temp
        return result

    def _multiply_scalar(self, digits, scalar):
        if scalar == 0:
            return [0] * self.num_digits
        result = [0] * (len(digits) + 12)
        carry = 0
        for i, d in enumerate(digits):
            temp = d * scalar + carry
            result[i] = temp % self.mask_base
            carry = temp // self.mask_base
        i = len(digits)
        while carry:
            result[i] = carry % self.mask_base
            carry //= self.mask_base
            i += 1
        return result[:self.num_digits + 8]

    def encode_byte(self, n_index=0):
        if n_index >= self.num_n_streams:
            n_index = 0
        n = self.Ns[n_index]
        for i in range(self.num_digits):
            n[i] = self.R[i]
        bit = random.randint(0, 255)
        n[0] += bit
        for i in range(self.num_digits - 1):
            if n[i] >= self.mask_base:
                n[i] -= self.mask_base
                n[i+1] += 1
        self.L = n[:]
        self._update_derived()

    def decode_byte(self, n_index=0):
        """Safe decode for alpha release"""
        if n_index >= self.num_n_streams:
            n_index = 0
        n = self.Ns[n_index]
        self.L = n[:]
        self._update_derived()
        diff = n[0] - self.R[0]
        if diff < 0:
            diff += self.mask_base
        recovered = diff % 256   # Safe byte
        self.L = n[:]
        self._update_derived()
        return recovered

    def print_state(self):
        print(f"L: {self.L}")
        print(f"R: {self.R}")
        print(f"D: {self.D}")
        print("-" * 50)
