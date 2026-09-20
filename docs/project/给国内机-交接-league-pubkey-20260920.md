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