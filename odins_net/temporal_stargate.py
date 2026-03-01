class TemporalStargate:
    """Plugin interface for future subatomic scanner/encoder hardware."""

    def scan_object(self, object_id: str) -> Dict:
        """Hardware scans physical object → returns raw quantum state data."""
        # Future hardware would implement this (e.g. quantum tomography scan)
        raise NotImplementedError("Hardware driver required")

    def encode_state(self, quantum_state: Dict) -> Dict:
        """Take raw quantum state → compress/encode into lattice coordinate."""
        # Use OdinsEye + AB + BNS to turn state into coord
        # This part we can write today
        pass

    def decode_state(self, coord: Dict) -> Dict:
        """Reverse: coord → reconstructed quantum state for printing."""
        pass

    def reconstruct_object(self, quantum_state: Dict, target_time: Optional[int] = None):
        """Future hardware prints the copy at desired time/space."""
        raise NotImplementedError("Reconstructor hardware required")
