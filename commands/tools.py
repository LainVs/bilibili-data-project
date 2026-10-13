"""工具箱命令：crack（CRC32 逆向）、doctor（系统体检）。"""
from __future__ import annotations


def run_crack(args) -> int:
    from crc32_crack.core import crack_candidates, crack_fast

    for h in args.hashes:
        h = h.strip().lower()
        if args.all:
            cands = crack_candidates(h)
            if cands:
                print(f"{h} -> {len(cands)} 个候选: {cands} (推荐主候选: {cands[0]})")
            else:
                print(f"{h} -> 未能破解")
        else:
            uid = crack_fast(h)
            if uid is not None:
                print(f"{h} -> UID: {uid}")
            else:
                print(f"{h} -> 未能破解")
    return 0


def run_doctor(args) -> int:
    from tools.doctor import print_doctor_report

    print_doctor_report()
    return 0
