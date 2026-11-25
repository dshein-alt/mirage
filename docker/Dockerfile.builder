FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG SD_REVISION=79dc7ca9074d79419a46380cdb95a4524db9b74c
ENV SD_REVISION=$SD_REVISION

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git libvulkan-dev glslc spirv-headers \
    ca-certificates curl gnupg python3 binutils && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key -o /tmp/nodesource-repo.gpg.key && \
    gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg /tmp/nodesource-repo.gpg.key && \
    rm /tmp/nodesource-repo.gpg.key && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && apt-get install -y --no-install-recommends nodejs && \
    npm install -g pnpm@10.15.1 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /sd.cpp
COPY stable-diffusion.cpp/ .

ENV CMAKE_FLAGS="-DSD_VULKAN=ON -DSD_BUILD_SHARED_LIBS=ON -DGGML_NATIVE=OFF -DSD_BUILD_SHARED_GGML_LIB=ON -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON -DCMAKE_INSTALL_RPATH=\$ORIGIN"
RUN cmake . -B ./build \
    -DSD_VULKAN=ON \
    -DSD_BUILD_SHARED_LIBS=ON \
    -DGGML_NATIVE=OFF \
    -DSD_BUILD_SHARED_GGML_LIB=ON \
    -DGGML_BACKEND_DL=ON \
    -DGGML_CPU_ALL_VARIANTS=ON \
    -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
    -DCMAKE_INSTALL_RPATH='$ORIGIN' && \
    cmake --build ./build --config Release -j$(nproc)

COPY docker/export-runtime.sh /opt/export-runtime.sh
RUN chmod 0755 /opt/export-runtime.sh
ENTRYPOINT ["/opt/export-runtime.sh"]
