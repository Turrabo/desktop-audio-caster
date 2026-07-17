"""M1 gate for the mirroring spike: prove the crypto port offline.

The RTCP checkpoint CANNOT catch a crypto bug (the receiver checkpoints on
packet completeness before decryption), so encryption must be proven here,
before any hardware probe. Shape ports openscreen frame_crypto_unittest.cc
plus a known-answer test on the nonce construction itself - the surface a
transcription slip would live on.
"""
import unittest

from experiments.mirroring.frame_crypto import FrameCrypto


class TestFrameCrypto(unittest.TestCase):
    KEY = bytes(range(16))
    MASK = bytes(range(100, 116))

    def test_round_trip_identity(self):
        fc = FrameCrypto(self.KEY, self.MASK)
        payload = bytes(range(256)) * 3 + b"tail"  # non-block-aligned
        for frame_id in (0, 1, 255, 256, 0xFFFFFFFF):
            self.assertEqual(
                fc.decrypt(frame_id, fc.encrypt(frame_id, payload)), payload)

    def test_same_payload_different_frame_id_differs(self):
        # Proves frame_id actually reaches the nonce (openscreen's test).
        fc = FrameCrypto(self.KEY, self.MASK)
        payload = b"identical payload" * 8
        self.assertNotEqual(fc.encrypt(1, payload), fc.encrypt(2, payload))

    def test_nonce_known_answer(self):
        # Hand-computed: zeros, frame_id big-endian at offset 8, XOR mask.
        mask = bytes([0xAA] * 16)
        fc = FrameCrypto(bytes(16), mask)
        # frame_id 0x01020304 -> bytes 8..11 = 01 02 03 04 before mask
        expected = bytearray([0xAA] * 16)
        expected[8] ^= 0x01
        expected[9] ^= 0x02
        expected[10] ^= 0x03
        expected[11] ^= 0x04
        self.assertEqual(fc.nonce(0x01020304), bytes(expected))
        # zero mask degenerates to the raw layout
        fc0 = FrameCrypto(bytes(16), bytes(16))
        self.assertEqual(
            fc0.nonce(0x01020304),
            bytes(8) + bytes([1, 2, 3, 4]) + bytes(4))

    def test_frame_id_truncates_to_32_bits(self):
        fc = FrameCrypto(bytes(16), bytes(16))
        self.assertEqual(fc.nonce(0x1_0000_0001), fc.nonce(1))

    def test_bad_key_lengths_rejected(self):
        with self.assertRaises(ValueError):
            FrameCrypto(bytes(15), bytes(16))
        with self.assertRaises(ValueError):
            FrameCrypto(bytes(16), bytes(17))


if __name__ == "__main__":
    unittest.main()
