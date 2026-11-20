/* Installed as /usr/bin/bash and /usr/bin/dash (so /bin/sh too): a shell the executor uid
 * starts by any path runs as the agent; everyone else gets the real shell (REAL). */
#include <stdio.h>
#include <unistd.h>

#define EXEC 10000

int main(int argc, char **argv) {
    (void)argc;
    if (getuid() == EXEC)
        execv("/opt/eval/bin/agent-exec", argv);
    else
        execv(REAL, argv);
    perror("exec-shell");
    return 127;
}
