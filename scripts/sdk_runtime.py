from pathlib import Path

ROOTFS_JAVA_HOME = "/usr/lib/jvm/java-21-openjdk-amd64"


def sandbox(runtime: Path | None, jdk: Path | None) -> tuple[list[str], str]:
    command = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
    ]
    if runtime is not None:
        command.extend(["--ro-bind", str(runtime), "/"])
        java_home = ROOTFS_JAVA_HOME
    else:
        if jdk is None:
            raise ValueError("Specify --jdk or --runtime")
        command.extend(["--ro-bind", "/usr", "/usr"])
        for name in ("/bin", "/sbin", "/lib", "/lib64"):
            if Path(name).is_symlink():
                command.extend(["--symlink", str(Path(name).readlink()), name])
            elif Path(name).is_dir():
                command.extend(["--ro-bind", name, name])
        command.extend(["--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache"])
        java_home = "/mnt/jdk"
    command.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/mnt"])
    if runtime is None:
        command.extend(["--ro-bind", str(jdk), java_home])
    return command, java_home
