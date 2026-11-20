/* Drop to the agent uid and start its shell. Only the executor group may run it; it holds
 * file capabilities for setuid/setgid and can become nothing but the agent. */
#define _GNU_SOURCE
#include <grp.h>
#include <stdio.h>
#include <sys/prctl.h>
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
    argv[0] = "bash";
    execv("/opt/eval/bin/agent-shell", argv);
    perror("agent-exec");
    return 127;
}
