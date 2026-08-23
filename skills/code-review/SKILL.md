---
name: code-review
description: 执行全面代码评审，覆盖安全、性能与可维护性分析。当用户要求审查代码、排查缺陷或审计代码库时使用。
---

# 代码评审技能

你现在具备进行全面代码评审的专业能力。请遵循以下结构化方法：

## 评审检查清单

### 1. 安全性（最高优先级）

检查：
- [ ] **注入漏洞**：SQL 注入、命令注入、XSS、模板注入
- [ ] **认证问题**：硬编码凭据、弱认证方案
- [ ] **授权缺陷**：缺失访问控制、IDOR（不安全直接对象引用）
- [ ] **数据暴露**：日志或错误信息中泄露敏感数据
- [ ] **密码学问题**：弱算法、不当密钥管理
- [ ] **依赖风险**：已知漏洞（使用 `npm audit`、`pip-audit` 检查）

```bash
# 快速安全扫描
npm audit                    # Node.js
pip-audit                    # Python
cargo audit                  # Rust
grep -r "password\|secret\|api_key" --include="*.py" --include="*.js"
```

### 2. 正确性

检查：
- [ ] **逻辑错误**：边界错误（off-by-one）、空值处理、边界场景
- [ ] **竞态条件**：并发访问缺少同步保护
- [ ] **资源泄漏**：文件、连接、内存未正确释放
- [ ] **错误处理**：吞异常、缺失错误分支
- [ ] **类型安全**：隐式转换、过度使用 any

### 3. 性能

检查：
- [ ] **N+1 查询**：循环内反复进行数据库调用
- [ ] **内存问题**：大对象分配、引用长期保留
- [ ] **阻塞操作**：异步流程中出现同步 I/O
- [ ] **低效算法**：可用 O(n) 却写成 O(n^2)
- [ ] **缺少缓存**：重复执行高开销计算

### 4. 可维护性

检查：
- [ ] **命名**：清晰、一致、具备语义
- [ ] **复杂度**：函数超过 50 行、嵌套超过 3 层
- [ ] **重复代码**：复制粘贴代码块
- [ ] **死代码**：未使用导入、不可达分支
- [ ] **注释质量**：过期、冗余，或关键处缺失注释

### 5. 测试

检查：
- [ ] **覆盖率**：关键路径是否有测试覆盖
- [ ] **边界场景**：空值、空字符串、边界值
- [ ] **Mock 策略**：外部依赖是否被隔离
- [ ] **断言质量**：断言是否具体且有意义

## 评审输出格式

```markdown
## 代码评审：[文件/组件名称]

### 总结
[1-2 句总体结论]

### 严重问题
1. **[问题名称]**（第 X 行）：[问题描述]
   - 影响：[可能造成的后果]
   - 修复建议：[建议方案]

### 改进建议
1. **[建议点]**（第 X 行）：[描述]

### 正向评价
- [做得好的地方]

### 评审结论
[ ] 可以合并
[ ] 需要小改
[ ] 需要大幅修改
```

## 常见高风险模式（应重点标注）

### Python
```python
# Bad: SQL 注入
cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
# Good:
cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))

# Bad: 命令注入
os.system(f"ls {user_input}")
# Good:
subprocess.run(["ls", user_input], check=True)

# Bad: 可变默认参数
def append(item, lst=[]):  # Bug: 共享可变默认值
# Good:
def append(item, lst=None):
    lst = lst or []
```

### JavaScript/TypeScript
```javascript
// Bad: 原型污染风险
Object.assign(target, userInput)
// Good:
Object.assign(target, sanitize(userInput))

// Bad: 使用 eval
eval(userCode)
// Good: 永远不要对用户输入使用 eval

// Bad: 回调地狱
getData(x => process(x, y => save(y, z => done(z))))
// Good:
const data = await getData();
const processed = await process(data);
await save(processed);
```

## 评审常用命令

```bash
# 查看最近改动
git diff HEAD~5 --stat
git log --oneline -10

# 查找潜在风险点
grep -rn "TODO\|FIXME\|HACK\|XXX" .
grep -rn "password\|secret\|token" . --include="*.py"

# 检查复杂度（Python）
pip install radon && radon cc . -a

# 检查依赖版本
npm outdated  # Node
pip list --outdated  # Python
```

## 评审工作流

1. **理解上下文**：阅读 PR 描述、关联 issue
2. **运行代码**：能构建就构建、能测试就测试、尽量本地跑
3. **自顶向下阅读**：先看入口与主流程
4. **检查测试**：是否覆盖改动？测试是否通过？
5. **安全扫描**：运行自动化工具
6. **人工审查**：按上方清单逐项检查
7. **输出反馈**：具体、可执行、给出修复建议并保持专业
