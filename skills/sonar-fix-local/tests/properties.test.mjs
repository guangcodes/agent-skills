import test from "node:test";
import assert from "node:assert/strict";
import { parseProperties, serializeCsvValues } from "../scripts/properties.mjs";

test("Properties 支持分隔符、注释、空值和最后一次赋值", () => {
  const result = parseProperties(" # comment\\\r\n! comment\rkey=old\r\nkey\t : final  \nempty:\nnoValue\ncolon:one:two\n");
  assert.deepEqual([...result], [
    ["key", "final"], ["empty", ""], ["noValue", ""], ["colon", "one:two"],
  ]);
});

test("Sonar 多值 CSV 保留字段逗号、双引号、换行和空白", () => {
  assert.equal(serializeCsvValues(["plain", "comma,name", 'quote"name', " padded ", "two\nlines"]),
    'plain,"comma,name","quote""name"," padded ","two\nlines"');
  assert.equal(serializeCsvValues([]), "");
  assert.throws(() => serializeCsvValues([{}]), /字符串数组/);
});

test("Properties 解码键和值的转义，保留值中的反斜线及特殊字符", () => {
  const result = parseProperties(String.raw`escaped\ key\:\==\tvalue\u002f**\nnext\\end\!`);
  assert.equal(result.get("escaped key:="), "value/**\nnext\\end!");
  assert.equal(parseProperties(String.raw`key=C:\\`).get("key"), "C:\\");
  assert.equal(parseProperties(String.raw`key=\u00a0value\u00a0`).get("key"), "\u00a0value\u00a0");
});

test("Properties 明确拒绝续行和无效 Unicode 转义", () => {
  assert.throws(() => parseProperties("key=one,\\\n two\n"), /不支持续行/);
  assert.throws(() => parseProperties("key=one,\\\r two\r"), /不支持续行/);
  assert.throws(() => parseProperties(String.raw`key=\u12XZ`), /Unicode/);
});

test("允许续行的读取按 Java 语义拼接键、值与后续缩进", () => {
  const text = "sonar.project\\\r\n  Key=first\nsonar.projectKey:final\\\r\n\t-key\nvalue=one\\\n #two\n";
  const properties = parseProperties(text, { allowContinuations: true });
  assert.equal(properties.get("sonar.projectKey"), "final-key");
  assert.equal(properties.get("value"), "one#two");
  assert.equal(parseProperties("key=value\\", { allowContinuations: true }).get("key"), "value");
});
