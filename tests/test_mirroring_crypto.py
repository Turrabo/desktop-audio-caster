"""Offline gate for the mirror crypto (streamer._aesctr, Windows CNG).

The RTCP checkpoint cannot catch a crypto bug (the receiver checkpoints on
packet completeness before decryption), so encryption is proven here before
any hardware use. Includes a known-answer vector computed from an independent
AES-CTR oracle, so the test does not depend on any third-party crypto library.
"""
import unittest

from streamer._aesctr import FrameCrypto


class TestFrameCrypto(unittest.TestCase):
    KEY = bytes(range(16))
    MASK = bytes(range(100, 116))

    def test_round_trip_identity(self):
        fc = FrameCrypto(self.KEY, self.MASK)
        payload = bytes(range(256)) * 3 + b"tail"   # not block-aligned
        for frame_id in (0, 1, 255, 256, 0xFFFFFFFF):
            self.assertEqual(
                fc.decrypt(frame_id, fc.encrypt(frame_id, payload)), payload)

    def test_same_payload_different_frame_id_differs(self):
        fc = FrameCrypto(self.KEY, self.MASK)
        payload = b"identical payload" * 8
        self.assertNotEqual(fc.encrypt(1, payload), fc.encrypt(2, payload))

    def test_nonce_known_answer(self):
        mask = bytes([0xAA] * 16)
        fc = FrameCrypto(bytes(16), mask)
        expected = bytearray([0xAA] * 16)
        for i, b in enumerate((0x01, 0x02, 0x03, 0x04)):
            expected[8 + i] ^= b
        self.assertEqual(fc.nonce(0x01020304), bytes(expected))
        fc0 = FrameCrypto(bytes(16), bytes(16))
        self.assertEqual(fc0.nonce(0x01020304),
                         bytes(8) + bytes([1, 2, 3, 4]) + bytes(4))

    def test_ciphertext_known_answer(self):
        # Independent oracle (see docstring); guards a transcription slip in
        # the CTR counter/keystream assembly, which the nonce KAT alone can't.
        key = bytes(range(16))
        mask = bytes(range(16, 32))
        fc = FrameCrypto(key, mask)
        ct = fc.encrypt(0x01020304, bytes(range(20)))
        self.assertEqual(
            ct.hex(), "2a13b373c488006431c68170359b70217ad5c82c")

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
