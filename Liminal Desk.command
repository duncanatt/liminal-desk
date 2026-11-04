#!/bin/zsh
cd "${0:A:h}" || exit 1
if [[ ! -x venv/bin/python ]]; then
    print 'Run the setup instructions in README.md first.'
    read '?Press Return to close.'
    exit 1
fi
exec venv/bin/python -m liminal_desk
