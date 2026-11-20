/* Start the agent's shell as the agent uid with an empty environment; the shell loads the
 * agent's. Only the executor group (and root) may run it; it holds file capabilities for
 * setuid/setgid and can become nothing but the agent. The shell runs in a child, and this
 * process, with no capabilities left, waits for it: the executor can kill this one, and the
 * kernel then kills the shell. Inherited sockets are closed: one opened as the executor
 * would keep the executor's network path. */
#define _GNU_SOURCE
#include <grp.h>
#include <linux/capability.h>
#include <signal.h>
#include <stdio.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#define AGENT 10001

static void close_sockets(void) {
    long max = sysconf(_SC_OPEN_MAX);
    struct stat st;
    for (int fd = 3; fd < (max > 0 && max < 65536 ? max : 65536); fd++)
        if (fstat(fd, &st) == 0 && S_ISSOCK(st.st_mode))
            close(fd);
}

int main(int argc, char **argv) {
    (void)argc;
    pid_t parent = getpid(), child = fork();
    if (child < 0) {
        perror("agent-exec");
        return 126;
    }
    if (child == 0) {
        gid_t groups[] = {AGENT};
        if (setgroups(1, groups) || setresgid(AGENT, AGENT, AGENT) ||
            setresuid(AGENT, AGENT, AGENT) || prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) ||
            prctl(PR_SET_PDEATHSIG, SIGKILL) || getppid() != parent) {
            perror("agent-exec");
            _exit(126);
        }
        close_sockets();
        char *empty[] = {NULL};
        argv[0] = "bash";
        execve("/opt/eval/bin/agent-shell", argv, empty);
        perror("agent-exec");
        _exit(127);
    }
    struct __user_cap_header_struct header = {_LINUX_CAPABILITY_VERSION_3, 0};
    struct __user_cap_data_struct none[2] = {{0}};
    if (syscall(SYS_capset, &header, none) || prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) {
        perror("agent-exec");
        kill(child, SIGKILL);
        return 126;
    }
    close(0);
    close(1);
    close(2);
    int status;
    while (waitpid(child, &status, 0) < 0)
        ;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
