/* Drop to the agent uid and start its shell with an empty environment; the shell loads the
 * agent's. Only the executor group (and root) may run it; it holds file capabilities for
 * setuid/setgid and can become nothing but the agent. Inherited sockets are closed: one
 * opened as the executor would keep the executor's network path. */
#define _GNU_SOURCE
#include <grp.h>
#include <stdio.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <unistd.h>

#define AGENT 10001

int main(int argc, char **argv) {
    (void)argc;
    gid_t groups[] = {AGENT};
    if (setgroups(1, groups) || setresgid(AGENT, AGENT, AGENT) || setresuid(AGENT, AGENT, AGENT) ||
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) {
        perror("agent-exec");
        return 126;
    }
    long max = sysconf(_SC_OPEN_MAX);
    struct stat st;
    for (int fd = 3; fd < (max > 0 && max < 65536 ? max : 65536); fd++)
        if (fstat(fd, &st) == 0 && S_ISSOCK(st.st_mode))
            close(fd);
    char *empty[] = {NULL};
    argv[0] = "bash";
    execve("/opt/eval/bin/agent-shell", argv, empty);
    perror("agent-exec");
    return 127;
}
