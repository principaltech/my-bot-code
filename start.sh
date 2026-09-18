#!/bin/bash

set -e

echo "======================================"
echo " SPLASH INTERNET BOT"
echo " Starting ZeroTier..."
echo "======================================"

mkdir -p /var/lib/zerotier-one

zerotier-one -d

echo "Waiting for ZeroTier..."
sleep 8

echo "Joining ZeroTier network..."

zerotier-cli join "$ZEROTIER_NETWORK_ID"

sleep 5

echo "ZeroTier status:"
zerotier-cli status

echo "ZeroTier networks:"
zerotier-cli listnetworks

echo "======================================"
echo " Starting Splash Internet bot..."
echo "======================================"

python bot.py