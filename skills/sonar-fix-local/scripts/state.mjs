import fs from "node:fs";
import path from "node:path";

export const read = (filename) => JSON.parse(fs.readFileSync(filename, "utf8"));
export function save(filename, value) {
  fs.mkdirSync(path.dirname(filename), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filename, JSON.stringify(value, null, 2) + "\n", {
    mode: 0o600,
  });
}
