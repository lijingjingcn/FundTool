// 从 stdin 读取反爬挑战脚本，执行后输出它设置的 cookie（用 ;; 分隔）
const chunks = [];
process.stdin.on("data", (c) => chunks.push(c));
process.stdin.on("end", () => {
  let src = Buffer.concat(chunks).toString("utf8");
  // 剥掉外层 <script>...</script> 标签
  const open = src.match(/<script[^>]*>/i);
  const close = src.lastIndexOf("</script>");
  if (open && close !== -1) {
    src = src.slice(open.index + open[0].length, close);
  }
  const cookies = [];
  const documentStub = {
    set cookie(v) {
      cookies.push(String(v));
    },
    get cookie() {
      return cookies.join("; ");
    },
  };
  const locationStub = { href: "" };
  try {
    const run = new Function(
      "document",
      "location",
      "setTimeout",
      "setInterval",
      "window",
      "navigator",
      src
    );
    run(documentStub, locationStub, () => {}, () => {}, {}, { userAgent: "node" });
  } catch (e) {
    // 挑战脚本尾部的页面重载调用可能抛错，cookie 已捕获即可
  }
  process.stdout.write(cookies.join(";;"));
});
