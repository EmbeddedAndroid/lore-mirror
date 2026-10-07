Return-Path: <sanderson@brivo.com>
X-Spam-Checker-Version: SpamAssassin 3.4.0 (2014-02-07) on
	aws-us-west-2-korg-lkml-1.web.codeaurora.org
Received: from aws-us-west-2-korg-lkml-1.web.codeaurora.org (localhost.localdomain [127.0.0.1])
	by smtp.lore.kernel.org (Postfix) with ESMTP id DE9C1CA5FCE
	for <webhook@archiver.kernel.org>; Mon,  5 Oct 2026 13:54:14 +0000 (UTC)
Received: from mail-yw1-f177.google.com (mail-yw1-f177.google.com [209.85.128.177])
 by mx.groups.io with SMTP id smtpd.msgproc01-g2.20616.1791208451430951743
 for <u-boot-amlogic@groups.io>;
 Mon, 05 Oct 2026 06:54:11 -0700
Authentication-Results: mx.groups.io;
 dkim=pass header.i=@brivo.com header.s=google header.b=O32TxxMu;
 spf=pass (domain: brivo.com, ip: 209.85.128.177, mailfrom: sanderson@brivo.com)
Received: by mail-yw1-f177.google.com with SMTP id 00721157ae682-8ab43af5e97so11343377b3.1
        for <u-boot-amlogic@groups.io>; Mon, 05 Oct 2026 06:54:11 -0700 (PDT)
DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed;
        d=brivo.com; s=google; t=1791208450; x=1791813250; darn=groups.io;
        h=content-transfer-encoding:mime-version:references:in-reply-to
         :message-id:date:subject:cc:to:from:from:to:cc:subject:date
         :message-id:reply-to:content-type;
        bh=/U+PnyT2iWh9/WvOCXsxIjB6wGFmSPn+xfP4fP8k/tA=;
        b=O32TxxMuW5omHV79VYgJMoSsjo+DzinYjaUFm2CsLh/NzORy2mhAIHnbj3tJMCOCNe
         pRk7WOJNRhBD1HPapNlLuVOd02VBqCCg/N45Ni8uLBA33rKnkUGIp4HS5oGzypzsyV5c
         MhobTXgDNMUOnbHGIbLEHgU6dH66nzCGVMvH7LhHV+fB7unstkD6V48AxGI6jBktR1CT
         JZvEy7wWvH0SbAAboQ4gRnGlMCjnvBkoHCCwoxQMO+BChhePESCyZ6EU4T5gPvMYo0jz
         jsnm8S6GsRKF5BqUiq1phyiQUis9Xe/ky874EF6DIMo8GjaFOKzrHyIFZRdjJxPaQJFc
         mv7g==
X-Google-DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed;
        d=1e100.net; s=20260707; t=1791208450; x=1791813250;
        h=content-transfer-encoding:mime-version:references:in-reply-to
         :message-id:date:subject:cc:to:from:x-gm-gg:x-gm-message-state:from
         :to:cc:subject:date:message-id:reply-to:content-type;
        bh=/U+PnyT2iWh9/WvOCXsxIjB6wGFmSPn+xfP4fP8k/tA=;
        b=WShWS1HlTiC5SweaiBPKw7qrFkDKxLCGkIuYyoF8+HHJ1GnsCq+ISxmhaabxMLQfYg
         M4iEfui2Q6WrTcfb3AOj/yEgWYj09CDN3z4NT3QosoWWadfGzpgxvpzmCgoF2aTn5c3o
         FwD9uZl+uEEvTi7R6LrTHGS1PQeIHBcoECzcNi9Xz/kWDc6a8ApIGYaB4GQ69t5nqdwD
         Kw1uvlUxokrtr2VkRTpEhK5/MFtw6V+JXoHIEoaO9EZFRzoB+UOoXZRuSoYarRhmDjrN
         SEw2jVO3qoJM7vZftWYX73F9Jq/691VUSr9A4b8cOr1dmsnsO4col/tM3J2CdEcpk6cS
         xuPA==
X-Forwarded-Encrypted: i=1; AKwUvBzQjmp57PROaq43BYCas26eGJ52gxvGxRjRXCOIh0sI3UvaIp0ZpBfqn7F/tmNpVPyx6ojQ71WQQi913iPzsg==@groups.io
X-Gm-Message-State: AFuF++kerM4WLhdfcu1zazWBZNoi+RbYjTjAFMTf3TZkcRSe2Wg6xnvk
	F17cEX4157MUorRHvjD7VQuFiC73po8JK4NfYLCr57k3edHQLBuTvrsobrEzom65Uc0qrx/4KdW
	565Czu/uI36cCcHA4VOE2QAoAkOJeIDIh739DqsWaznFSsVaTBL1uzA==
X-Gm-Gg: AYBFou3NbY/l9uRefbw1ZwZtGB+TNGPGQ61xB9spGPj7J9LsAjWCs5BsPMLSOGluW+c
	ED0CSmAAIMynBNfAjyB8dC0aixxnOjsEFmvGs+p9tZt7lR1BE6zkhgQmoXg+eYPHFI/4pAmJvmt
	oAUC3YlWEftKhJfqbk2HRldRBFe+icES8QiHWAHbTrj4gASJdvqNajvGeT2HzRgJxCA73iPeHCO
	hWIDSMYO4dtIV4W/GLtkRNJd5/KfljSOJr2e0xYAqiCCgGTvsCPwEmoYoiB2T3RgIiyjTV0aadf
	/e9R0qLY56e9aUy6kZSarVIEkR7KcR3Nzj1oarUv0oRU5UJteEUk+RNIrxlB77GBte2s4DUQuPp
	gPP/x25dhjN47hcCV0i4LD4Kp+h1kNqB7ZWM54yVIGDrpFNma0MVk3SRcTXPr8cvwah9xOaSYJ1
	PPERVspKlrTWVUsKFJpCO+x7Xem6B39Zydha4D0G918UO6APjDK8aUx5mgAcmvS7IQ
X-Received: by 2002:a05:690c:95:b0:873:5c7b:c132 with SMTP id 00721157ae682-8acdaf00b28mr62547427b3.26.1791208450288;
        Mon, 05 Oct 2026 06:54:10 -0700 (PDT)
Received: from strozzi ([66.193.28.125])
        by smtp.gmail.com with ESMTPSA id 00721157ae682-8ae30120b8csm38852867b3.4.2026.10.05.06.54.09
        (version=TLS1_3 cipher=TLS_AES_256_GCM_SHA384 bits=256/256);
        Mon, 05 Oct 2026 06:54:10 -0700 (PDT)
From: Sean Anderson <sanderson@brivo.com>
To: Jerome Forissier <jerome.forissier@arm.com>,
	u-boot@lists.u-boot-project.org
Cc: Ramon Fried <rfried.dev@gmail.com>,
	Tom Rini <trini@konsulko.com>,
	Neil Armstrong <neil.armstrong@linaro.org>,
	u-boot-amlogic@groups.io,
	Sean Anderson <sanderson@brivo.com>,
	Joe Hershberger <joe.hershberger@ni.com>
Subject: [PATCH v2 2/2] net: designware: Fix MDIO bus getting bound multiple times
Date: Mon,  5 Oct 2026 09:54:03 -0400
Message-ID: <20261005135406.509901-3-sanderson@brivo.com>
X-Mailer: git-send-email 2.53.0
In-Reply-To: <20261005135406.509901-1-sanderson@brivo.com>
References: <20261005135406.509901-1-sanderson@brivo.com>
MIME-Version: 1.0
Content-Transfer-Encoding: 8bit
List-Id: <u-boot-amlogic.groups.io>
X-Webhook-Received: from 45-33-107-173.ip.linodeusercontent.com [45.33.107.173] by
 aws-us-west-2-korg-lkml-1.web.codeaurora.org with HTTPS for
 <u-boot-amlogic@groups.io>; Mon, 05 Oct 2026 13:54:14 -0000
X-Groupsio-URL: https://groups.io/g/u-boot-amlogic/message/3116

designware_eth_probe may be called multiple times for a given device.
such as if the first probe (or post_probe) fails for whatever reason.
When this happens, subsequent probes still try to bind the MDIO bus,
creating several duplicate copies of the bus (all possibly sharing the
same of_node). This can cause an infinite loop in dm_mdio_probe_devices
if we have been probed at least once already:

dm_mdio_probe_devices()
 device_probe(dwmac.mac0)
  device_probe(dwmac)
   designware_eth_probe()
    device_bind_driver_to_node(dwmac.mac1)
    return -EFOO
 device_probe(dwmac.mac1)
  ...

Prevent this by binding the MDIO bus in designware_eth_bind instead of
designware_eth_probe.

Fixes: 5160b4567c71 ("net: designware: add DM_MDIO support")
Signed-off-by: Sean Anderson <sanderson@brivo.com>
---

Changes in v2:
- Fix warnings about mdio_err and dw_mdio_init not being used
- Fix missing else clause when bitbang MDIO is enabled

 drivers/net/designware.c    | 30 ++++++++++++++++--------------
 drivers/net/designware.h    |  1 +
 drivers/net/dwmac_meson8b.c |  3 +++
 drivers/net/dwmac_s700.c    |  3 +++
 drivers/net/dwmac_socfpga.c |  3 +++
 drivers/net/dwmac_thead.c   |  3 +++
 drivers/net/gmac_rockchip.c |  3 +++
 7 files changed, 32 insertions(+), 14 deletions(-)

diff --git a/drivers/net/designware.c b/drivers/net/designware.c
index ff7294ab4f4..2ca21c373db 100644
--- a/drivers/net/designware.c
+++ b/drivers/net/designware.c
@@ -210,15 +210,14 @@ static int dw_mdio_init(const char *name, void *priv)
 	return mdio_register(bus);
 }
 
-static int dw_dm_mdio_init(const char *name, void *priv)
+int dw_dm_mdio_bind(struct udevice *dev)
 {
-	struct udevice *dev = priv;
+	struct udevice *mdiodev;
 	ofnode node;
 	int ret;
 
 	ofnode_for_each_subnode(node, dev_ofnode(dev)) {
 		const char *subnode_name = ofnode_get_name(node);
-		struct udevice *mdiodev;
 
 		if (strcmp(subnode_name, "mdio"))
 			continue;
@@ -232,8 +231,10 @@ static int dw_dm_mdio_init(const char *name, void *priv)
 	}
 
 	printf("%s: mdio node is missing, registering legacy mdio bus", __func__);
-
-	return dw_mdio_init(name, priv);
+	ret = device_bind_driver(dev, "eth_designware_mdio", dev->name, &mdiodev);
+	if (ret)
+		debug("%s: not able to create mdio bus\n", __func__);
+	return ret;
 }
 
 #if IS_ENABLED(CONFIG_BITBANGMII) && IS_ENABLED(CONFIG_DM_GPIO)
@@ -855,6 +856,8 @@ static int designware_eth_bind(struct udevice *dev)
 		}
 	}
 
+	if (IS_ENABLED(CONFIG_DM_MDIO))
+		return dw_dm_mdio_bind(dev);
 	return 0;
 }
 
@@ -962,16 +965,15 @@ int designware_eth_probe(struct udevice *dev)
 	} else
 #endif
 	{
-		if (IS_ENABLED(CONFIG_DM_MDIO))
-			ret = dw_dm_mdio_init(dev->name, dev);
-		else
-			ret = dw_mdio_init(dev->name, dev);
-		if (ret) {
-			err = ret;
-			goto mdio_err;
-		}
-		priv->bus = miiphy_get_dev_by_name(dev->name);
 		priv->dev = dev;
+		if (!IS_ENABLED(CONFIG_DM_MDIO)) {
+			ret = dw_mdio_init(dev->name, dev);
+			if (ret) {
+				err = ret;
+				goto mdio_err;
+			}
+			priv->bus = miiphy_get_dev_by_name(dev->name);
+		}
 	}
 
 	ret = dw_phy_init(priv, dev);
diff --git a/drivers/net/designware.h b/drivers/net/designware.h
index cccf9d54e02..3618300f4c5 100644
--- a/drivers/net/designware.h
+++ b/drivers/net/designware.h
@@ -249,6 +249,7 @@ struct dw_eth_dev {
 	struct mii_dev *bus;
 };
 
+int dw_dm_mdio_bind(struct udevice *dev);
 int designware_eth_of_to_plat(struct udevice *dev);
 int designware_eth_probe(struct udevice *dev);
 extern const struct eth_ops designware_eth_ops;
diff --git a/drivers/net/dwmac_meson8b.c b/drivers/net/dwmac_meson8b.c
index fde4aabbace..cb9de81a819 100644
--- a/drivers/net/dwmac_meson8b.c
+++ b/drivers/net/dwmac_meson8b.c
@@ -157,6 +157,9 @@ U_BOOT_DRIVER(dwmac_meson8b) = {
 	.id		= UCLASS_ETH,
 	.of_match	= dwmac_meson8b_ids,
 	.of_to_plat = dwmac_meson8b_of_to_plat,
+#if IS_ENABLED(CONFIG_DM_MDIO)
+	.bind		= dw_dm_mdio_bind,
+#endif
 	.probe		= dwmac_meson8b_probe,
 	.ops		= &designware_eth_ops,
 	.priv_auto	= sizeof(struct dw_eth_dev),
diff --git a/drivers/net/dwmac_s700.c b/drivers/net/dwmac_s700.c
index 76daab961c0..d95f2111a5b 100644
--- a/drivers/net/dwmac_s700.c
+++ b/drivers/net/dwmac_s700.c
@@ -56,6 +56,9 @@ U_BOOT_DRIVER(dwmac_s700) = {
 	.id     = UCLASS_ETH,
 	.of_match = dwmac_s700_ids,
 	.of_to_plat = dwmac_s700_of_to_plat,
+#if IS_ENABLED(CONFIG_DM_MDIO)
+	.bind		= dw_dm_mdio_bind,
+#endif
 	.probe  = dwmac_s700_probe,
 	.ops    = &designware_eth_ops,
 	.priv_auto	= sizeof(struct dw_eth_dev),
diff --git a/drivers/net/dwmac_socfpga.c b/drivers/net/dwmac_socfpga.c
index a55f3e29f9f..64bb2dbfbe3 100644
--- a/drivers/net/dwmac_socfpga.c
+++ b/drivers/net/dwmac_socfpga.c
@@ -143,6 +143,9 @@ U_BOOT_DRIVER(dwmac_socfpga) = {
 	.id		= UCLASS_ETH,
 	.of_match	= dwmac_socfpga_ids,
 	.of_to_plat = dwmac_socfpga_of_to_plat,
+#if IS_ENABLED(CONFIG_DM_MDIO)
+	.bind		= dw_dm_mdio_bind,
+#endif
 	.probe		= dwmac_socfpga_probe,
 	.ops		= &designware_eth_ops,
 	.priv_auto	= sizeof(struct dw_eth_dev),
diff --git a/drivers/net/dwmac_thead.c b/drivers/net/dwmac_thead.c
index 138d71a6202..5843bed49cb 100644
--- a/drivers/net/dwmac_thead.c
+++ b/drivers/net/dwmac_thead.c
@@ -280,6 +280,9 @@ U_BOOT_DRIVER(dwmac_thead) = {
 	.id		= UCLASS_ETH,
 	.of_match	= dwmac_thead_match,
 	.of_to_plat	= dwmac_thead_of_to_plat,
+#if IS_ENABLED(CONFIG_DM_MDIO)
+	.bind		= dw_dm_mdio_bind,
+#endif
 	.probe		= dwmac_thead_probe,
 	.ops		= &dwmac_thead_eth_ops,
 	.priv_auto	= sizeof(struct dw_eth_dev),
diff --git a/drivers/net/gmac_rockchip.c b/drivers/net/gmac_rockchip.c
index 2b6080dd9ee..1d21cb2606d 100644
--- a/drivers/net/gmac_rockchip.c
+++ b/drivers/net/gmac_rockchip.c
@@ -790,6 +790,9 @@ U_BOOT_DRIVER(eth_gmac_rockchip) = {
 	.id	= UCLASS_ETH,
 	.of_match = rockchip_gmac_ids,
 	.of_to_plat = gmac_rockchip_of_to_plat,
+#if IS_ENABLED(CONFIG_DM_MDIO)
+	.bind		= dw_dm_mdio_bind,
+#endif
 	.probe	= gmac_rockchip_probe,
 	.ops	= &gmac_rockchip_eth_ops,
 	.priv_auto	= sizeof(struct dw_eth_dev),
-- 
2.53.0


