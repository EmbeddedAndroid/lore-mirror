Return-Path: <sanderson@brivo.com>
X-Spam-Checker-Version: SpamAssassin 3.4.0 (2014-02-07) on
	aws-us-west-2-korg-lkml-1.web.codeaurora.org
Received: from aws-us-west-2-korg-lkml-1.web.codeaurora.org (localhost.localdomain [127.0.0.1])
	by smtp.lore.kernel.org (Postfix) with ESMTP id E11EACA5FF0
	for <webhook@archiver.kernel.org>; Mon,  5 Oct 2026 13:54:14 +0000 (UTC)
Received: from mail-yx2-f40.google.com (mail-yx2-f40.google.com [74.125.224.168])
 by mx.groups.io with SMTP id smtpd.msgproc02-g2.20629.1791208449722978416
 for <u-boot-amlogic@groups.io>;
 Mon, 05 Oct 2026 06:54:09 -0700
Authentication-Results: mx.groups.io;
 dkim=pass header.i=@brivo.com header.s=google header.b=en6O45t3;
 spf=pass (domain: brivo.com, ip: 74.125.224.168, mailfrom: sanderson@brivo.com)
Received: by mail-yx2-f40.google.com with SMTP id 00721157ae682-8a87fe69060so11027917b3.3
        for <u-boot-amlogic@groups.io>; Mon, 05 Oct 2026 06:54:09 -0700 (PDT)
DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed;
        d=brivo.com; s=google; t=1791208449; x=1791813249; darn=groups.io;
        h=content-transfer-encoding:mime-version:message-id:date:subject:cc
         :to:from:from:to:cc:subject:date:message-id:reply-to:content-type;
        bh=KBEIEFcMcXzsgW2+ZMyV4vVgAP+RBI8vO6R+mODtMqA=;
        b=en6O45t3gfj8QxZq3HM/rSa/X7txcBRo7zDD48Yz9/a2NVfTkw3YbvDDPH4L6fK7i1
         exwN8JxJsu1YncbH80tmZTf2Q0Eanf9z+qwGR1HtTe5qQOdkrdaeHRe/Dx47wA52la/i
         qOt4S4GbsYYiA6OBKxfwPK+l0VsKZsLHLwa6VPoAud/mIn9jIUq96vP5Kyx7GHabM5Pj
         LXtnDbwx8ClYqdyfG/Xn1xaMYmwJN0RPuQtPbpNKHox503rb24wfKYaopBkaRTa9ni0u
         MhsPzmYK/JjTBWV1nc8o1QKGDVdp+NSiUlsvmEA3ZmBVWr0hEtzjyWXfd90oQB6CHVOF
         ymAg==
X-Google-DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed;
        d=1e100.net; s=20260707; t=1791208449; x=1791813249;
        h=content-transfer-encoding:mime-version:message-id:date:subject:cc
         :to:from:x-gm-gg:x-gm-message-state:from:to:cc:subject:date
         :message-id:reply-to:content-type;
        bh=KBEIEFcMcXzsgW2+ZMyV4vVgAP+RBI8vO6R+mODtMqA=;
        b=2Hf4tXBZpkfdf+aGr87rQAVE4EJCsTyqpHwFFz1FDSLNiyl448DEtaaYGxP/y0HCD4
         9UAv4kkTewKr8oWK2g9rDgyW8Wc9XNHHkrieyYyPZaCxj/g3AK5w7v4WBnH6FRM+dkO2
         XplTZSzaYtOPJ6ikPhROMLAcV+f2OvTh/fJM+dvhVtjkKD/jEHBh3reqWhoaPL+b8Mim
         IMD2zndQj4Z8XzC/OjCO5uh9ME6/ZQinYwnWBQj4E2fJW0QxczKFwhMupMW1DHsVuWhi
         csgSw1XyuR+KUJnlb2N90Vrra26Dn7tPhjpMJynUwI6VmR9G0dbkzJ/TOtIG0i3cF9Ju
         ZcuQ==
X-Forwarded-Encrypted: i=1; AKwUvByRI/EWqDOuJMTTywls+QHTSPs9z34oK7EF5Yt1YPJphwqGRpPXRUmT5jo1eZUHA38uQFzjAe9n9jey6qr6Ow==@groups.io
X-Gm-Message-State: AFuF++lRo+UUut//7vnViFBm3s1HY6PyORk+0VoZCqiBKaGex6I1q5+a
	3S1pBhzcl3NzqcUMHHQAvOX+WhGe3QbX1vzF2qMP8D0JhoSOb+iI+y8AxI1gA6fFTmI8NLpUfWf
	q8S7QfcCvBlxbH2aWN0IzT8rPa8vv0J6fPQsCHFqm0X+K5re259rGyg==
X-Gm-Gg: AYBFou3f5k89UontWuqy/iphfDAS8rQ5LW26g8Gi8IUrwgFCsGh7YBkjaUxiUdcbf/n
	dgMFbS28e1OIVUjFwoCqQ7vhLR5Ks5y2SdhZJn99NR3L4YGfoV/zYDx0+sP1yLvUA2CavL5IwvO
	xPVvH9SFkL34vM/DcqVDKiJiAwMmQRcbdGqvDJSt8Cw8IYux9gLVN9M+0KXUL4iUv2XNkZm6Wnu
	QBYZOu/Zzo8qZ9LGqMVtSwgw6bW8lcdLDDA9rR8zUORqrs7OwPg+ZtOkzor3GUFWPvySG/7b3Dr
	5O1fsdZfL2W0t50l8VsLM56nFmq4eiuA8BKRYoAMg3jTI557v9i/Img5uc4jaOrgNyIIq0GD+B8
	YOn8JCkvMeBb1yvP9RT7ve4X3TV3PFA3lf7i83+02FhJJLu7kN0Bv06FjWtmSnrK0BYWo7MiDWT
	70FXUr4KbUM+0u6m5qmDqnKB836LJ5UqchKvmNNOq5zI+fM1nf2gnFq3ciBHgdN+g=
X-Received: by 2002:a05:690c:e3ee:b0:897:7280:3dd3 with SMTP id 00721157ae682-8ae392af13emr43691677b3.21.1791208448597;
        Mon, 05 Oct 2026 06:54:08 -0700 (PDT)
Received: from strozzi ([66.193.28.125])
        by smtp.gmail.com with ESMTPSA id 00721157ae682-8ae30120b8csm38852867b3.4.2026.10.05.06.54.07
        (version=TLS1_3 cipher=TLS_AES_256_GCM_SHA384 bits=256/256);
        Mon, 05 Oct 2026 06:54:08 -0700 (PDT)
From: Sean Anderson <sanderson@brivo.com>
To: Jerome Forissier <jerome.forissier@arm.com>,
	u-boot@lists.u-boot-project.org
Cc: Ramon Fried <rfried.dev@gmail.com>,
	Tom Rini <trini@konsulko.com>,
	Neil Armstrong <neil.armstrong@linaro.org>,
	u-boot-amlogic@groups.io,
	Sean Anderson <sanderson@brivo.com>,
	Joe Hershberger <joe.hershberger@ni.com>
Subject: [PATCH v2 0/2] net: designware: Fix MDIO bus getting bound multiple times
Date: Mon,  5 Oct 2026 09:54:01 -0400
Message-ID: <20261005135406.509901-1-sanderson@brivo.com>
X-Mailer: git-send-email 2.53.0
MIME-Version: 1.0
Content-Transfer-Encoding: 8bit
List-Id: <u-boot-amlogic.groups.io>
X-Webhook-Received: from 45-33-107-173.ip.linodeusercontent.com [45.33.107.173] by
 aws-us-west-2-korg-lkml-1.web.codeaurora.org with HTTPS for
 <u-boot-amlogic@groups.io>; Mon, 05 Oct 2026 13:54:14 -0000
X-Groupsio-URL: https://groups.io/g/u-boot-amlogic/message/3114

This series fixes a bug caused by the MDIO bus getting created in
probe() instead of bind().

Changes in v2:
- Use if instead of #if for DM_MDIO
- Fix warnings about mdio_err and dw_mdio_init not being used
- Fix missing else clause when bitbang MDIO is enabled

Sean Anderson (2):
  net: designware: Use if instead of #if for DM_MDIO
  net: designware: Fix MDIO bus getting bound multiple times

 drivers/net/designware.c    | 71 +++++++++++++++++++------------------
 drivers/net/designware.h    |  1 +
 drivers/net/dwmac_meson8b.c |  3 ++
 drivers/net/dwmac_s700.c    |  3 ++
 drivers/net/dwmac_socfpga.c |  3 ++
 drivers/net/dwmac_thead.c   |  3 ++
 drivers/net/gmac_rockchip.c |  3 ++
 7 files changed, 52 insertions(+), 35 deletions(-)

---
base-commit: 2b8902913c19eb581fb0d567201c09b8fa46ac74
branch: dw_mdio_bind

-- 
2.53.0


