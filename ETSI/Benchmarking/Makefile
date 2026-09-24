PREFIX ?= /home/bonsai/dcmb/.tlmsp
CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -Werror
CPPFLAGS += -I$(PREFIX)/include
LDFLAGS += -L$(PREFIX)/lib -Wl,-rpath,$(PREFIX)/lib
LDLIBS += -lcurl

.PHONY: all clean

all: tlmsp_loadgen

tlmsp_loadgen: tlmsp_loadgen.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $< $(LDFLAGS) $(LDLIBS) -o $@

clean:
	rm -f tlmsp_loadgen persistent_client
