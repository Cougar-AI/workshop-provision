FROM ubuntu:25.04

ENV DEBIAN_FRONTEND=noninteractive

# ---- Base desktop + xrdp ----
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    apt-get update && apt-get install -y --no-install-recommends \
    xfce4 xfce4-terminal \
    xrdp \
    xorgxrdp \
    sudo \
    nano \
    dbus \
    dbus-x11 \
    curl \
    wget \
    gpg \
    ca-certificates \
    git \
    python3 \
    python3-pip \
    python3-venv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /usr/share/doc/* /usr/share/man/*

 
# ---- Install uv (fast Rust-based Python package manager) ----
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
 
# ---- Workshop Python venv (built from requirements.txt via uv) ----
COPY requirements.txt /tmp/requirements.txt
RUN uv venv /opt/workshop-venv && \
    uv pip install --python /opt/workshop-venv/bin/python --no-cache -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt && \
    find /opt/workshop-venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
    find /opt/workshop-venv -type d -name "tests" -exec rm -rf {} + 2>/dev/null; \
    find /opt/workshop-venv -type d -name "test" -exec rm -rf {} + 2>/dev/null; \
    true

# ---- VS Code (official Microsoft repo) ----
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    wget -qO- https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /usr/share/keyrings/packages.microsoft.gpg && \
    install -D -o root -g root -m 644 /usr/share/keyrings/packages.microsoft.gpg /etc/apt/keyrings/packages.microsoft.gpg && \
    sh -c 'echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main" > /etc/apt/sources.list.d/vscode.list' && \
    apt-get update && apt-get install -y --no-install-recommends code && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /usr/share/doc/* /usr/share/man/*

# ---- xrdp session config (with the D-Bus fix) ----
RUN echo '#!/bin/sh\n\
if test -r /etc/profile; then\n\
    . /etc/profile\n\
fi\n\
if test -r ~/.profile; then\n\
    . ~/.profile\n\
fi\n\
unset DBUS_SESSION_BUS_ADDRESS\n\
eval $(dbus-launch --sh-syntax --exit-with-session)\n\
startxfce4' > /etc/xrdp/startwm.sh && \
    chmod +x /etc/xrdp/startwm.sh

RUN echo "allowed_users=anybody" > /etc/X11/Xwrapper.config && \
    echo "needs_root_rights=yes" >> /etc/X11/Xwrapper.config

# ---- Create the workshop user ----
RUN useradd -m -s /bin/bash -c "Workshop-test" caiworkshopstest && \
    echo "caiworkshopstest:CAI_2026!" | chpasswd && \
    echo "caiworkshopstest ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers && \
    adduser caiworkshopstest ssl-cert

# ---- Auto-activate the venv for the workshop user's shells ----
RUN echo '\nsource /opt/workshop-venv/bin/activate' >> /home/caiworkshopstest/.bashrc && \
    chown caiworkshopstest:caiworkshopstest /home/caiworkshopstest/.bashrc

# ---- Register the venv as a Jupyter kernel ----
RUN /opt/workshop-venv/bin/python -m ipykernel install --name workshop-venv --display-name "Workshop (Python)" --prefix=/opt/workshop-venv

# ---- Tell VS Code to default to this interpreter ----
RUN mkdir -p /home/caiworkshopstest/.config/Code/User && \
    echo '{\n  "python.defaultInterpreterPath": "/opt/workshop-venv/bin/python"\n}' > /home/caiworkshopstest/.config/Code/User/settings.json && \
    chown -R caiworkshopstest:caiworkshopstest /home/caiworkshopstest/.config

# ---- Install VS Code extensions for the workshop user (Python + Jupyter) ----
# --user-data-dir avoids needing an active X session for this install step
RUN su - caiworkshopstest -c "code --no-sandbox --user-data-dir /home/caiworkshopstest/.vscode-setup \
    --install-extension ms-python.python \
    --install-extension ms-toolsai.jupyter \
    --force" && \
    rm -rf /home/caiworkshopstest/.vscode-setup

RUN sed -i 's|Exec=.*/code|& --no-sandbox|' /usr/share/applications/code.desktop || true

RUN mkdir -p /var/run/dbus

EXPOSE 3389

CMD mkdir -p /var/run/dbus && dbus-daemon --system --fork && xrdp-sesman && xrdp --nodaemon
