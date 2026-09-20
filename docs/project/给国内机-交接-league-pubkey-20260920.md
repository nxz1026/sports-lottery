# 给国内采集机：league 账号 pubkey 交接（2026-09-20）

> 用途：请国内采集机把自己**实机 SSH 公钥**发来，我追加到 oracle 的 `league` 受限账号授权后，
> 10 分钟档推送改为走正式 key-only 登录（当前链路仍通，但 `authorized_keys` 是 0 字节，属于临时态）。

## 交给 oracle（用普通文本，不需要大文件）

请提供**国内采集机**用于连接 oracle 的 SSH 公钥，`pub` 那一行，注明是哪台主机名/用户。

格式示例（换行无所谓）：

```
ssh-ed25519 AAAA… user@collector-machine   <!-- 采  样 -->
ssh-rsa AAAAB3NzaC1yc2E… user@host          <!-- 备选 -->
```

## 我这边收到后会做（无需你操作）

1. 追加到 `/srv/league-staging/.ssh/authorized_keys`，每行带强制前缀：
   `command="/usr/local/bin/league-rsync-shell"`, `restrict`, `no-port-forwarding` 等。
2. 复测：仅放行 `rsync/scp -t` 传 JSONL；交互 shell / scp 取文件 / 越界写 / 命令走私全拒。
3. 改完会告诉你，10 分钟档随之切到 key-only。

## 为什么现在要

- oracle 上 `league` 账号 + 受限 shell + `/srv/league-staging/{incoming,done,bad}` 均已在 2026-09-15 建好，
  链路现有数据在进（最近批 owner=league）。
- 但 `/srv/league-staging/.ssh/authorized_keys` 目前 **0 字节**（E2E 验证后密钥已清，只留了受限 shell）。
  正式投入需要你这边实机公钥，否则每次连都依赖临时 fallback。

---

## 交接进度（2026-09-21 归档）

### 国内机侧回话（ND-PC-WIN → oracle，2026-09-20）

- **变更链路**：`scp + sudo`（ubuntu 全权限）→ **`tar` 流式**（league 专用 key + 受限 shell 白名单）
- **公钥**：`ssh-ed25519 AAAAC3…ZPZ ND-PC-WIN league cn-collector`
  指纹 `SHA256:wj5a7V6Eq5/bsL04tIjpRDceQVrZe3yxjEapRdEg7io`
  来源：ND-PC-WIN（ND 机）采集机 cn-collector 专用 keypair（`collector-league`），非交互 ssh 专用。
- **oracle 侧配套改动（国内机已做，已生效）**：
  1. 受限 shell 白名单扩展：`/usr/local/bin/league-rsync-shell` 新增 `tar -C DIR -xzf -` 分支；备份
     `/tmp/league-rsync-shell.bak-202609210623`，回滚命令见国内机回话。
  2. `authorized_keys` 强制前缀行：`command="/usr/local/bin/league-rsync-shell",restrict,no-pty,no-port-forwarding,
     no-X11-forwarding,no-agent-forwarding ssh-ed25519 AAAAC3…ZPZ ND-PC-WIN league cn-collector`；
     权限 600 league:league、父目录 700 league:league。
  3. 推送目标 `/srv/league-staging/incoming/cn-collector/<topic>/<ts>__NNN.jsonl` + 顶层 `<ts>Z.done`，league:league 属主。
- **国内机侧改动（collector-cn，commit 94e9924）**：`config.json` 增 `push.ssh_alias=oracle-league`；
  `collector.py` `push_batch` 本地 `tar -czf -` → 管道 `ssh oracle-league "tar -C <incoming>/cn-collector -xzf -"`，
  无 /tmp / sudo / staging/mv；`.done` 以最终时间戳名先落本地 `out/` 再随流推（受限 shell 无法远端改名）。
- **已验证**：7 topic 快批 + 8 topic 夜批（含 jc_odds_history）各实跑，远端数据 + `.done` 全部落位，
  属主 league:league，auth.log `accept(tar)` 2 次/批；单例锁 / 日志 / 计划任务定义不变。

### 海外侧（oracle）确认（2026-09-21 复核，全部通过）

| 待确认项 | 结论 | 实测证据 |
|---|---|---|
| `authorized_keys` 前缀行=最终形态 | ✅ 采纳锁定 | 本机当前内容与国内机一致：`command=…,restrict,no-*` + 公钥；权限 600/700 league |
| 白名单 tar 分支是否接受 | ✅ 接受保留 | `/usr/local/bin/league-rsync-shell` 含 `tar -C… -xzf -` 分支；备份 `bak-202609210623` 在位；链路稳定，无需装 rsync/回滚 |
| `.done` 消费对新属主无感 | ✅ 确认 | 装载器 cron（ubuntu 跑）可读 league:league 目录与 `.done`；最近装载 22:30/22:40 `rc=0`，count jc_offer=20000 / jc_issue_match=246 / jc_issue_prize=31 |

**收尾结论**：league key-only + 受限 shell（tar 流式）链路，海外侧全部确认到位；数据正常入港入库。
P0-infra 收起。