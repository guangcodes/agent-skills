const whitespace = /[ \t\f]/;

// Scanner 的多值配置按 RFC 4180 解析，不能把单个值中的逗号当作分隔符。
export function serializeCsvValues(values) {
  return values.map((value) => {
    if (typeof value !== "string") throw Error("Sonar 多值配置必须为字符串数组");
    return /[",\r\n]|^\s|\s$/.test(value)
      ? '"' + value.replaceAll('"', '""') + '"'
      : value;
  }).join(",");
}

function unescape(value) {
  let result = "";
  for (let i = 0; i < value.length; i++) {
    if (value[i] !== "\\") {
      result += value[i];
      continue;
    }
    const escaped = value[++i];
    if (escaped === "u") {
      const hex = value.slice(i + 1, i + 5);
      if (!/^[\da-fA-F]{4}$/.test(hex))
        throw Error("无效 Sonar properties Unicode 转义");
      result += String.fromCharCode(Number.parseInt(hex, 16));
      i += 4;
    } else {
      result += { t: "\t", r: "\r", n: "\n", f: "\f" }[escaped] ?? escaped ?? "";
    }
  }
  return result;
}

// 与 Scanner 的 java.util.Properties.load + String.trim 语义保持一致；
// 续行仍按当前流程的限制明确拒绝，不静默改变分析范围。
export function parseProperties(text, { allowContinuations = false } = {}) {
  const properties = new Map();
  const lines = text.split(/\r\n|\n|\r/);
  for (let row = 0; row < lines.length; row++) {
    let line = lines[row].replace(/^[ \t\f]+/, "");
    if (!line || line.startsWith("#") || line.startsWith("!")) continue;
    while ((line.match(/\\+$/)?.[0].length ?? 0) % 2) {
      if (!allowContinuations)
        throw Error("暂不支持续行 Sonar properties；不能静默改变扫描范围");
      line = line.slice(0, -1);
      if (++row >= lines.length) break;
      line += lines[row].replace(/^[ \t\f]+/, "");
    }
    let end = 0;
    while (end < line.length) {
      if (line[end] === "\\") end += 2;
      else if (whitespace.test(line[end]) || line[end] === "=" || line[end] === ":")
        break;
      else end++;
    }
    let start = end;
    while (start < line.length && whitespace.test(line[start])) start++;
    if (line[start] === "=" || line[start] === ":") start++;
    while (start < line.length && whitespace.test(line[start])) start++;
    properties.set(
      unescape(line.slice(0, end)),
      unescape(line.slice(start)).replace(/^[\x00-\x20]+|[\x00-\x20]+$/g, ""),
    );
  }
  return properties;
}
