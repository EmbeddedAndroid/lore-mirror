Return-Path: <sanderson@brivo.com>
X-Spam-Checker-Version: SpamAssassin 3.4.0 (2014-02-07) on
	aws-us-west-2-korg-lkml-1.web.codeaurora.org
Received: from aws-us-west-2-korg-lkml-1.web.codeaurora.org (localhost.localdomain [127.0.0.1])
	by smtp.lore.kernel.org (Postfix) with ESMTP id DFC8CCA5FFE
	for <webhook@archiver.kernel.org>; Mon,  5 Oct 2026 13:54:14 +0000 (UTC)
Received: from mail-yx2-f41.google.com (mail-yx2-f41.google.com [74.125.224.169])
 by mx.groups.io with SMTP id smtpd.msgproc02-g2.20630.1791208450562651126
 for <u-boot-amlogic@groups.io>;
 Mon, 05 Oct 2026 06:54:10 -0700
Authentication-Results: mx.groups.io;
 dkim=pass header.i=@brivo.com header.s=google header.b=uQWhIEpw;
 spf=pass (domain: brivo.com, ip: 74.125.224.169, mailfrom: sanderson@brivo.com)
Received: by mail-yx2-f41.google.com with SMTP id 00721157ae682-8ab748519f0so14096917b3.1
        for <u-boot-amlogic@groups.io>; Mon, 05 Oct 2026 06:54:10 -0700 (PDT)
DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed;
        d=brivo.com; s=google; t=1791208449; x=1791813249; darn=groups.io;
        h=content-transfer-encoding:mime-version:references:in-reply-to
         :message-id:date:subject:cc:to:from:from:to:cc:subject:date
         :message-id:reply-to:content-type;
        bh=8ITs71KRgsgmpgc9LjGcs5P8uwwsWBif4lD4OtkhNr4=;
        b=uQWhIEpwIYg163A//Mi9eS1OssKj0GpTaKgrffegUMVC/b5mB+EPdVy7PTluK0UeOz
         kIdM3WyFbaEi/FBCQz+/Klfav7FMrkvEt4S+nHxLOKNGLYT8rwTwBlOeEPUf8B9WEqYf
         wPsWsS8pWbqMCCpawRybTK56ZlzRy+YYQMlEuYHtx9YijOGrrnavldJtswsPPgIl7aZi
         0QHYMCxnZDTEdUb0VuweaYQ/R48hAw1xp7aE3i31RsT2rIa6soMbKt7TavxmEZN23x5X
         mkJkrzHmiAH7mepKPVTJRJGE7YEZyhjo1N5Cx4v6SETuaLemBfDsAOFAc448TslqIxqD
         vrBw==
X-Google-DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed;
        d=1e100.net; s=20260707; t=1791208449; x=1791813249;
        h=content-transfer-encoding:mime-version:references:in-reply-to
         :message-id:date:subject:cc:to:from:x-gm-gg:x-gm-message-state:from
         :to:cc:subject:date:message-id:reply-to:content-type;
        bh=8ITs71KRgsgmpgc9LjGcs5P8uwwsWBif4lD4OtkhNr4=;
        b=lQUvK5K46VG/mMfnL6cfwHq6GYyjG2rBtmuOLw0zfRoL50V5+ZkjP2GaKMyVZJb92T
         6PXlH6PAs5Dh4SCVNj8zFUS7zKgKrWa+jGPjdx3WJsZXBg3OrsiWec2cvv18M8lkfICv
         EgYkDx3K93HruUEiWKXecmBhZogRdPB+zn3UfcTY6AuqiysyOcQACg7YtgsLv+kCLQ1m
         zXrsk86rBnP2sapehw30h8JDdNLoX3tsO/wY/8Qa5HA0S/n8NN9JxB6sQHQ8ZfS3RKdP
         SwMjsMx4ufwAHP5f9LZLLF0fH2mNFwxDhCruhdfLe5irBsVSF8OUq+/LbuI7ZTpsQYW/
         6E8w==
X-Forwarded-Encrypted: i=1; AKwUvBwhInkgetplglxHyGgoyTCKfhIaDJhB3c99L4WzlXjFzb9M5M8kAKN8bNDl9DUo4RN2p4yCpUjGCHOAsPEOhQ==@groups.io
X-Gm-Message-State: AFq9FYIb8KZawfmAE2VS15rEeRXaVu0tbCK22vWHhE998DImSP7ZQpmB
	gBCDVA56nRtsvdFXVyXCO5IkESg27Do7Oq0hylO51flTY0TbKQ3QIG1fn5dBzcby6vbFE36eWnt
	+qrfXl/w7ZKtZz1HGzYKMu1etkAIrs1HNBFgxrblHbJFvzKSL0Uh5YA==
X-Gm-Gg: AYBFou0c1sc+jJypLTp/2+RIHkRiw0fABGGz4c1wZCnuDaLZ+AxVUbpofUb57XRmFuJ
	xbLVYEKw2Gxdn7kDyI6qOAVhheqF2AC5gLstM/wBzmyrJrAW/WCC3jWQGWy1+YCvedpJumOSstD
	geCA3rqFrRDi2xbE+53RQgVey3KgzZ74KPclQJBNwdrbIUzodbYW5Rj9R/XzxqurWvS6kK3XWo7
	ak16q+tl+VQgcz2B2lltZ5yelgu+/MN2g+aOcc5SWt8JC37mKqL/5xUtHVI0do/8xsTVGh+ZZkD
	KPiPMiZNotLLibEyqc5ySvN8/NqJD/3emTCOLhC9rue3m7XUmnn9kbyiEo2dFZ7r61xHx+T6MKT
	pg5vnwP2o0F64/VCsNEX3PnGtAQD5qqs3gC+2K/+I6DuQgrRbMPg9BUIhesIiubXDRckWeKCm/6
	Ig38jtvqVX/iuWRXUZrBPEuZc+0A0g324EWeT+Uc8Zd/5U8katy2q1v8QEYszcNvA=
X-Received: by 2002:a05:690c:6d83:b0:879:44a6:1076 with SMTP id 00721157ae682-8ae397b974bmr41470347b3.32.1791208449366;
        Mon, 05 Oct 2026 06:54:09 -0700 (PDT)
Received: from strozzi ([66.193.28.125])
        by smtp.gmail.com with ESMTPSA id 00721157ae682-8ae30120b8csm38852867b3.4.2026.10.05.06.54.08
        (version=TLS1_3 cipher=TLS_AES_256_GCM_SHA384 bits=256/256);
        Mon, 05 Oct 2026 06:54:09 -0700 (PDT)
From: Sean Anderson <sanderson@brivo.com>
To: Jerome Forissier <jerome.forissier@arm.com>,
	u-boot@lists.u-boot-project.org
Cc: Ramon Fried <rfried.dev@gmail.com>,
	Tom Rini <trini@konsulko.com>,
	Neil Armstrong <neil.armstrong@linaro.org>,
	u-boot-amlogic@groups.io,
	Sean Anderson <sanderson@brivo.com>,
	Joe Hershberger <joe.hershberger@ni.com>
Subject: [PATCH v2 1/2] net: designware: Use if instead of #if for DM_MDIO
Date: Mon,  5 Oct 2026 09:54:02 -0400
Message-ID: <20261005135406.509901-2-sanderson@brivo.com>
X-Mailer: git-send-email 2.53.0
In-Reply-To: <20261005135406.509901-1-sanderson@brivo.com>
References: <20261005135406.509901-1-sanderson@brivo.com>
MIME-Version: 1.0
Content-Transfer-Encoding: 8bit
List-Id: <u-boot-amlogic.groups.io>
X-Webhook-Received: from 45-33-107-173.ip.linodeusercontent.com [45.33.107.173] by
 aws-us-west-2-korg-lkml-1.web.codeaurora.org with HTTPS for
 <u-boot-amlogic@groups.io>; Mon, 05 Oct 2026 13:54:14 -0000
X-Groupsio-URL: https://groups.io/g/u-boot-amlogic/message/3115

Where possible, convert ifdefs on DM_MDIO into ifs.

Signed-off-by: Sean Anderson <sanderson@brivo.com>
---

Changes in v2:
- New

 drivers/net/designware.c | 49 ++++++++++++++++++++--------------------
 1 file changed, 24 insertions(+), 25 deletions(-)

diff --git a/drivers/net/designware.c b/drivers/net/designware.c
index 6ed9c6d538a..ff7294ab4f4 100644
--- a/drivers/net/designware.c
+++ b/drivers/net/designware.c
@@ -137,15 +137,16 @@ static int dw_bb_mdio_reset(struct mii_dev *bus)
 
 #endif
 
-#if IS_ENABLED(CONFIG_DM_MDIO)
-int designware_eth_mdio_read(struct udevice *mdio_dev, int addr, int devad, int reg)
+static int designware_eth_mdio_read(struct udevice *mdio_dev, int addr,
+				    int devad, int reg)
 {
 	struct mdio_perdev_priv *pdata = dev_get_uclass_priv(mdio_dev);
 
 	return dw_mdio_read(pdata->mii_bus, addr, devad, reg);
 }
 
-int designware_eth_mdio_write(struct udevice *mdio_dev, int addr, int devad, int reg, u16 val)
+static int designware_eth_mdio_write(struct udevice *mdio_dev, int addr,
+				     int devad, int reg, u16 val)
 {
 	struct mdio_perdev_priv *pdata = dev_get_uclass_priv(mdio_dev);
 
@@ -162,7 +163,7 @@ int designware_eth_mdio_reset(struct udevice *mdio_dev)
 }
 #endif
 
-static const struct mdio_ops designware_eth_mdio_ops = {
+static __maybe_unused const struct mdio_ops designware_eth_mdio_ops = {
 	.read = designware_eth_mdio_read,
 	.write = designware_eth_mdio_write,
 #if CONFIG_IS_ENABLED(DM_GPIO)
@@ -170,7 +171,7 @@ static const struct mdio_ops designware_eth_mdio_ops = {
 #endif
 };
 
-static int designware_eth_mdio_probe(struct udevice *dev)
+static __maybe_unused int designware_eth_mdio_probe(struct udevice *dev)
 {
 	/* Use the priv data of parent */
 	dev_set_priv(dev, dev_get_priv(dev->parent));
@@ -178,6 +179,7 @@ static int designware_eth_mdio_probe(struct udevice *dev)
 	return 0;
 }
 
+#if CONFIG_IS_ENABLED(DM_MDIO)
 U_BOOT_DRIVER(designware_eth_mdio) = {
 	.name = "eth_designware_mdio",
 	.id = UCLASS_MDIO,
@@ -208,7 +210,6 @@ static int dw_mdio_init(const char *name, void *priv)
 	return mdio_register(bus);
 }
 
-#if IS_ENABLED(CONFIG_DM_MDIO)
 static int dw_dm_mdio_init(const char *name, void *priv)
 {
 	struct udevice *dev = priv;
@@ -234,7 +235,6 @@ static int dw_dm_mdio_init(const char *name, void *priv)
 
 	return dw_mdio_init(name, priv);
 }
-#endif
 
 #if IS_ENABLED(CONFIG_BITBANGMII) && IS_ENABLED(CONFIG_DM_GPIO)
 static int dw_eth_bb_mdio_active(struct mii_dev *miidev)
@@ -757,24 +757,24 @@ static int dw_phy_init(struct dw_eth_dev *priv, void *dev)
 	if (IS_ENABLED(CONFIG_DM_ETH_PHY))
 		eth_phy_set_mdio_bus(dev, NULL);
 
-#if IS_ENABLED(CONFIG_DM_MDIO)
-	phydev = dm_eth_phy_connect(dev);
-	if (!phydev)
-		return -ENODEV;
-#else
-	int phy_addr = -1;
+	if (IS_ENABLED(CONFIG_DM_MDIO)) {
+		phydev = dm_eth_phy_connect(dev);
+		if (!phydev)
+			return -ENODEV;
+	} else {
+		int phy_addr = -1;
 
-	if (IS_ENABLED(CONFIG_DM_ETH_PHY))
-		phy_addr = eth_phy_get_addr(dev);
+		if (IS_ENABLED(CONFIG_DM_ETH_PHY))
+			phy_addr = eth_phy_get_addr(dev);
 
 #ifdef CONFIG_PHY_ADDR
-	phy_addr = CONFIG_PHY_ADDR;
+		phy_addr = CONFIG_PHY_ADDR;
 #endif
 
-	phydev = phy_connect(priv->bus, phy_addr, dev, priv->interface);
-	if (!phydev)
-		return -ENODEV;
-#endif
+		phydev = phy_connect(priv->bus, phy_addr, dev, priv->interface);
+		if (!phydev)
+			return -ENODEV;
+	}
 
 	phydev->supported &= PHY_GBIT_FEATURES;
 	if (priv->max_speed) {
@@ -962,11 +962,10 @@ int designware_eth_probe(struct udevice *dev)
 	} else
 #endif
 	{
-#if IS_ENABLED(CONFIG_DM_MDIO)
-		ret = dw_dm_mdio_init(dev->name, dev);
-#else
-		ret = dw_mdio_init(dev->name, dev);
-#endif
+		if (IS_ENABLED(CONFIG_DM_MDIO))
+			ret = dw_dm_mdio_init(dev->name, dev);
+		else
+			ret = dw_mdio_init(dev->name, dev);
 		if (ret) {
 			err = ret;
 			goto mdio_err;
-- 
2.53.0


