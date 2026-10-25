# Frame checksum (device protocol rev C)

Every frame carries a one-byte checksum computed over the payload:

    checksum(payload) = (sum of payload byte values + 3 * len(payload)) mod 97

Two special rules from the legacy devices that rev C keeps for
compatibility:

* An **empty payload** has checksum **97** (the sentinel value, NOT 0).
* The result is otherwise always in the range 0..96.
