import { expect, it } from "vitest";
import { WsReassembler } from "../src/ee2-host";

function maskedFrame(
  payload: Buffer | string,
  opcode = 0x1,
  fin = true,
  mask: Buffer = Buffer.from([1, 2, 3, 4]),
): Buffer {
  const data = Buffer.isBuffer(payload) ? payload : Buffer.from(payload, "utf8");
  let header: Buffer;
  if (data.length < 126) {
    header = Buffer.from([(fin ? 0x80 : 0) | opcode, 0x80 | data.length]);
  } else {
    header = Buffer.alloc(4);
    header[0] = (fin ? 0x80 : 0) | opcode;
    header[1] = 0x80 | 126;
    header.writeUInt16BE(data.length, 2);
  }
  const masked = Buffer.from(data);
  for (let i = 0; i < masked.length; i++) masked[i] ^= mask[i % 4];
  return Buffer.concat([header, mask, masked]);
}

it("delivers a complete single-frame message", () => {
  const r = new WsReassembler();
  const { messages, closed } = r.push(maskedFrame('{"name":"x"}'));
  expect(messages).toEqual(['{"name":"x"}']);
  expect(closed).toBe(false);
});

it("reassembles a frame split across TCP chunks", () => {
  const r = new WsReassembler();
  const frame = maskedFrame(JSON.stringify({ name: "save-config", payload: { contents: "{}" } }));
  const first = r.push(frame.subarray(0, 5));
  expect(first.messages).toEqual([]);
  const second = r.push(frame.subarray(5));
  expect(second.messages).toEqual([
    JSON.stringify({ name: "save-config", payload: { contents: "{}" } }),
  ]);
});

it("joins fragmented messages across continuation frames", () => {
  const r = new WsReassembler();
  const part1 = maskedFrame('{"name":"save-', 0x1, false);
  const part2 = maskedFrame('config"}', 0x0, true);
  const first = r.push(part1);
  expect(first.messages).toEqual([]);
  const second = r.push(part2);
  expect(second.messages).toEqual(['{"name":"save-config"}']);
});

it("handles two frames arriving in one chunk", () => {
  const r = new WsReassembler();
  const both = Buffer.concat([maskedFrame('"a"'), maskedFrame('"b"')]);
  expect(r.push(both).messages).toEqual(['"a"', '"b"']);
});

it("reports close frames", () => {
  const r = new WsReassembler();
  const { closed } = r.push(maskedFrame("", 0x8, true));
  expect(closed).toBe(true);
});

it("drops the stream when the buffer overflows", () => {
  const r = new WsReassembler();
  // An unterminated giant frame: header(4) + mask(4) + 65535 payload, sent
  // repeatedly without ever completing within the cap.
  const huge = Buffer.alloc(70000, 0x41);
  const header = Buffer.alloc(4);
  header[0] = 0x81;
  header[1] = 0x80 | 127;
  const len = Buffer.alloc(8);
  len.writeBigUInt64BE(BigInt(Number.MAX_SAFE_INTEGER - 1), 0);
  const frameStart = Buffer.concat([header, len, Buffer.from([1, 2, 3, 4])]);
  let closed = false;
  for (let i = 0; i < 100 && !closed; i++) {
    closed = r.push(i === 0 ? Buffer.concat([frameStart, huge]) : huge).closed;
  }
  expect(closed).toBe(true);
});
