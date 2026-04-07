# cc-jsonl-fix

[English](README.md)

Claude Code 会话 JSONL 文件修复工具，解决 resume 时丢失对话历史的问题。

## 解决什么问题？

Claude Code 将对话以 JSONL 格式存储，消息通过 `uuid` / `parentUuid` 字段形成链表。`resume` 功能沿此链从最后一条消息反向遍历。链一旦断裂，resume 只能加载断裂点之后的少量消息，大量历史丢失。

这是一个[已知 bug](https://github.com/anthropics/claude-code/issues/22526)，在多个 issue 中被报告：

- [#22526](https://github.com/anthropics/claude-code/issues/22526) — resume 后只剩最后几条消息
- [#24304](https://github.com/anthropics/claude-code/issues/24304) — parentUuid 指向不存在的 UUID
- [#21751](https://github.com/anthropics/claude-code/issues/21751) — assistant 消息只写了 thinking block，文本部分丢失

## 修复了什么？

工具分四个阶段修复，每阶段针对一种特定损坏模式：

### Phase 1: 清洗

**NUL 字节清理** — 清除写入中断导致的 `\x00` 字节填充。底层 JSON 内容通常完好，剥离 NUL 后即可正常解析。

### Phase 1b: 快照碰撞修复

**messageId 去重** — `file-history-snapshot` 条目的 `messageId` 经常与真实消息的 `uuid` 碰撞。若 Claude Code 将两者放入同一索引，快照会遮蔽真实消息导致链断裂。此阶段将碰撞的 `messageId` 置为 null。

### Phase 2: 幽灵引用修复

**幽灵 UUID 修复** — 找到 `parentUuid` 指向文件中不存在的 UUID 的消息（"幽灵"可能因竞态条件未被写入）。重新指向最近的有效 `user`/`assistant` 消息，且保证不跨越 `compact_boundary` 压缩边界。

### Phase 3: 主链最大化

**分支吸收** — 修复幽灵引用后，主链可能仍跳过大段对话（它们在断链的分支上）。此阶段在每个分叉点找到不在主链中的兄弟分支，按分支大小贪心地重新连接到主链中。

```
修复前:
  主链:     ... → M → P → ...     （仅 5 条消息可达）
  丢失分支: P → S → ... → T       （988 条消息，断开）

修复后:
  ... → M → T → ... → S → P → ... （所有消息串成一条链）
  （M.parentUuid 从 P 改为 T）
```

## 使用方法

**环境要求**：Python >= 3.10，无外部依赖。

```bash
# 基本修复（自动创建备份）
uv run repair_jsonl.py ~/.claude/projects/<项目哈希>/<session-id>.jsonl

# 使用 uv（推荐）
uv run repair_jsonl.py <session-file>.jsonl

# 预览模式，不修改文件
uv run repair_jsonl.py <session-file>.jsonl --dry-run --verbose

# 输出到不同文件
uv run repair_jsonl.py <session-file>.jsonl -o repaired.jsonl

# 推荐：修复 + 更新时间戳（详见下文）
uv run repair_jsonl.py <session-file>.jsonl --touch
```

### 选项

| 参数 | 说明 |
|---|---|
| `-o, --output PATH` | 输出到指定文件（不覆盖原文件） |
| `--no-backup` | 跳过自动备份 |
| `--dry-run` | 仅分析报告，不修改文件 |
| `--verbose` | 显示每行修复细节 |
| `--touch` | 将最后一条消息的时间戳更新为当前时间 |
| `--force` | 即使完整性检查失败也强制写入 |

### 关于 `--touch`

通常你是在尝试 resume 时才发现会话损坏——历史消息丢失了。此时 JSONL 文件中最后一条消息的时间戳可能已经是数小时甚至数天前的。修复后再次 resume 时，Claude Code 会比较最后消息的时间戳与当前时间，如果间隔过大会提示：

```
This session is 9h 7m old and 256.9k tokens.
Resuming the full session will consume a substantial portion of your usage limits.

❯ 1. Resume from summary (recommended)
  2. Resume full session as-is
  3. Don't ask me again
```

这不是错误——你可以放心选择 **"Resume full session as-is"**，修复后的会话会正常加载。但如果你想跳过这个提示，修复时加上 `--touch`，它会把最后一条消息的时间戳更新为当前时间，Claude Code 就会认为这是一个"新鲜"的会话，直接 resume。

### 找到你的会话文件

```bash
# 列出项目的会话文件
ls ~/.claude/projects/<项目哈希>/

# 项目哈希由项目路径派生，例如：
# /home/user/myproject → F--home-user-myproject
# Windows: C:\Users\me\project → C--Users-me-project
```

### 输出示例

```
==================================================
  JSONL Repair Report
==================================================
Input:  18c635d0-e120-46bf-adbf-3b4709b4e43e.jsonl
Lines:  5667  |  UUID entries: 4908

--- Phase 1: Sanitize ---
  NUL-corrupted lines fixed: 1
  Snapshot messageId collisions fixed: 626

--- Phase 2: Fix Orphan parentUuids ---
  Orphans fixed: 1

--- Phase 3: Maximize Main Chain ---
  Branches absorbed: 31
  Messages absorbed: 1620

--- Verification ---
  Orphan parentUuids: 0  [PASS]
  Duplicate UUIDs:    0  [PASS]
  Cycles detected:    No  [PASS]
  Main chain length:  1701  (before: 5)
  Chain growth:       +1696 messages (+33920%)
==================================================
```

## 注意事项

**备份** — 工具会在修改前自动创建带时间戳的备份文件（如 `session.backup_20260404_152403.jsonl`）。如果想先预览，使用 `--dry-run`。

**完整性检查** — 修复后工具会验证结果（孤儿引用、循环、重复 UUID）。如果任何检查失败，除非指定 `--force`，否则拒绝写入输出文件。

**此 bug 可能会被官方修复** — Anthropic 已知晓这些问题。未来版本的 Claude Code 可能修复根因（JSONL 写入时的竞态条件）。届时此工具对新会话不再需要，但仍可修复已损坏的旧文件。

**不做保证** — 工具只修改 `parentUuid` 和 `messageId` 字段来修复链完整性，不会修改消息内容、thinking block 或签名。但修复后的会话行为可能与原始未损坏状态有差异。请务必保留备份。

**给工具开发者** — 如果你在开发 Claude Code 会话管理工具（查看器、导出器、分析工具），本项目中的链遍历和损坏检测逻辑可以作为参考。

## 相关 Issue

- [anthropics/claude-code#22526](https://github.com/anthropics/claude-code/issues/22526) — resume 时 parentUuid 链断裂
- [anthropics/claude-code#24304](https://github.com/anthropics/claude-code/issues/24304) — parentUuid 引用不存在的 UUID 和快照碰撞
- [anthropics/claude-code#21751](https://github.com/anthropics/claude-code/issues/21751) — 使用 extended thinking 时 assistant 文本部分丢失

## 局限性与反馈

本工具基于一个真实的损坏会话文件开发和测试，覆盖了我遇到的所有损坏模式，但我没有见过所有可能的 JSONL 损坏形式。如果本工具无法修复你的会话，欢迎[提交 issue](https://github.com/ymonster/cc_jsonl_fix/issues)，描述你遇到的问题（如果有相关的 Claude Code issue 链接也请附上）。我很乐意研究并尝试支持新的损坏模式。

## 许可证

MIT
