import { expect, it } from "vitest";
import { decodeClientFrames, encodeServerFrame } from "../src/ee2-host";

/**
 * Build a masked client text frame by hand (RFC 6455: client-to-server frames
 * MUST be masked). Payload length picks the 7-bit / 16-bit / 64-bit form.
 */
function encodeMaskedClientFrame(
  message: string,
  mask: Buffer = Buffer.from([0x12, 0x34, 0x56, 0x78]),
  opcode = 0x1,
): Buffer {
  const payload = Buffer.from(message, "utf8");
  let header: Buffer;
  if (payload.length < 126) {
    header = Buffer.from([0x80 | opcode, 0x80 | payload.length]);
  } else if (payload.length < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 0x80 | 126;
    header.writeUInt16BE(payload.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 0x80 | 127;
    header.writeBigUInt64BE(BigInt(payload.length), 2);
  }
  const masked = Buffer.from(payload);
  for (let i = 0; i < masked.length; i++) masked[i] ^= mask[i % 4];
  return Buffer.concat([header, mask, masked]);
}

/** Re-mask a server frame into a client frame so encode→decode roundtrips. */
function maskServerFrame(frame: Buffer, mask: Buffer): Buffer {
  let len = frame[1] & 0x7f;
  let payloadStart = 2;
  if (len === 126) {
    len = frame.readUInt16BE(2);
    payloadStart = 4;
  } else if (len === 127) {
    len = Number(frame.readBigUInt64BE(2));
    payloadStart = 10;
  }
  const header = Buffer.from(frame.subarray(0, payloadStart));
  header[1] |= 0x80; // set the mask bit
  const masked = Buffer.from(frame.subarray(payloadStart));
  for (let i = 0; i < masked.length; i++) masked[i] ^= mask[i % 4];
  return Buffer.concat([header, mask, masked]);
}

it("roundtrips encodeServerFrame through decodeClientFrames once masked", () => {
  const mask = Buffer.from([0xaa, 0xbb, 0xcc, 0xdd]);
  for (const message of [
    '{"name":"item-text","payload":"ünïcode"}', // 7-bit length
    "x".repeat(300), // 16-bit extended length
    "y".repeat(70_000), // 64-bit extended length
  ]) {
    const frame = encodeServerFrame(message);
    expect(frame[0]).toBe(0x81); // FIN + text opcode
    expect(frame[1] & 0x80).toBe(0); // server frames are unmasked
    expect(decodeClientFrames(maskServerFrame(frame, mask))).toEqual([message]);
  }
});

it("decodes a hand-built masked client text frame", () => {
  const frame = encodeMaskedClientFrame('{"hello":"world"}');
  expect(frame[1] & 0x80).toBe(0x80); // mask bit set
  expect(decodeClientFrames(frame)).toEqual(['{"hello":"world"}']);
});

it("refuses unmasked client frames", () => {
  // Same bytes as a valid server text frame — but clients MUST mask, so the
  // decoder stops at the first unmasked frame.
  expect(decodeClientFrames(encodeServerFrame("nope"))).toEqual([]);
});

it("decodes multiple frames in one buffer in order", () => {
  const buffer = Buffer.concat([
    encodeMaskedClientFrame("first"),
    encodeMaskedClientFrame("z".repeat(200), Buffer.from([1, 2, 3, 4])),
    encodeMaskedClientFrame("third"),
  ]);
  expect(decodeClientFrames(buffer)).toEqual([
    "first",
    "z".repeat(200),
    "third",
  ]);
});

it("returns complete frames and drops a trailing partial frame", () => {
  // Pins the current behavior: the decoder does not buffer partial frames
  // across chunks; an incomplete trailing frame is silently discarded.
  const complete = encodeMaskedClientFrame("whole");
  const partial = encodeMaskedClientFrame("truncated");
  expect(
    decodeClientFrames(
      Buffer.concat([complete, partial.subarray(0, partial.length - 3)]),
    ),
  ).toEqual(["whole"]);
  // A lone partial frame decodes to nothing.
  expect(decodeClientFrames(partial.subarray(0, 4))).toEqual([]);
  expect(decodeClientFrames(Buffer.alloc(0))).toEqual([]);
});

it("stops at a close frame and skips non-text opcodes", () => {
  const close = encodeMaskedClientFrame("", undefined, 0x8);
  const afterClose = Buffer.concat([close, encodeMaskedClientFrame("late")]);
  expect(decodeClientFrames(afterClose)).toEqual([]);

  // Binary frames are consumed but not surfaced; decoding continues after.
  const binaryThenText = Buffer.concat([
    encodeMaskedClientFrame("ignored-binary", undefined, 0x2),
    encodeMaskedClientFrame("text"),
  ]);
  expect(decodeClientFrames(binaryThenText)).toEqual(["text"]);
});
