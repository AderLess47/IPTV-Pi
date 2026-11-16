################################################################################
#
# IPTV-Pi br2-external tree
#
# No custom packages are needed: the image is assembled from upstream
# Buildroot packages, the board files in board/raspberrypi and the Kodi/
# web interface files under rootfs-overlay/.
#
# If you ever add packages, drop them in package/<name>/ and uncomment:
#
# include $(sort $(wildcard $(BR2_EXTERNAL_IPTVPI_PATH)/package/*/*.mk))
#
################################################################################
