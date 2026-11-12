<?php

$wgSitename = "RuneScape Wiki (2011)";
$wgMetaNamespace = "RuneScape_Wiki_(2011)";

# The address people reach it at: the hosted copy sets MW_SERVER in .env.
$wgServer = getenv("MW_SERVER") ?: "http://localhost:8090";
$wgScriptPath = "";
$wgArticlePath = "/wiki/$1";

# Credit: the text and images are the RuneScape Wiki's (runescape.wiki), under its licence.
$wgRightsUrl = "https://creativecommons.org/licenses/by-nc-sa/3.0/";
$wgRightsText = "CC BY-NC-SA 3.0, from the RuneScape Wiki (runescape.wiki) as it stood on 23 May 2011";

# Database
$wgDBtype = "mysql";
$wgDBserver = "db";
$wgDBname = "rswiki";
$wgDBuser = "wiki";
$wgDBpassword = getenv("MW_DB_PASSWORD") ?: "wiki_pw";
$wgDBTableOptions = "ENGINE=InnoDB, DEFAULT CHARSET=binary";

# The hosted copy sets its own in .env; nobody signs in, so they guard little.
$wgSecretKey = getenv("MW_SECRET_KEY") ?: "change-me-in-production";
$wgUpgradeKey = getenv("MW_UPGRADE_KEY") ?: "change-me-in-production";

# Performance
$wgMainCacheType = CACHE_ACCEL;
$wgSessionCacheType = CACHE_DB;
$wgJobRunRate = 0;
$wgEnableSidebarCache = true;
# Anonymous page views come from saved HTML (images/cache) instead of a fresh render:
# the hosted copy shares one CPU with another site.
$wgUseFileCache = true;

# API
$wgEnableAPI = true;
$wgEnableWriteAPI = false;

# Skin — classic Vector
wfLoadSkin( 'Vector' );
$wgDefaultSkin = 'vector';
$wgVectorDefaultSkinVersion = '1';

# No image logo — site name displays as text
$wgHooks['BeforePageDisplay'][] = function ( $out ) {
    $out->addInlineStyle( '
        #p-logo a { background-image: none !important; text-indent: 0 !important;
            color: #333; font-size: 13px; font-weight: bold; line-height: 1.2;
            display: flex; align-items: center; justify-content: center;
            text-align: center; padding: 8px; }
        #p-logo a::after { content: "RuneScape Wiki (2011)"; }
        #p-logo { height: auto !important; min-height: 40px; }
        #pt-login, #pt-createaccount, #pt-anonuserpage, #pt-anontalk { display: none; }
    ' );
};

# Extensions (bundled in Docker image)
wfLoadExtension( 'ParserFunctions' );
$wgPFEnableStringFunctions = true;
wfLoadExtension( 'Cite' );
wfLoadExtension( 'CategoryTree' );
wfLoadExtension( 'TemplateData' );
// wfLoadExtension( 'Variables' ); // not bundled with the mediawiki image; restore by installing it into a mounted extensions/ dir

# Read-only reference wiki
$wgGroupPermissions['*']['edit'] = false;
$wgGroupPermissions['*']['createaccount'] = false;
$wgGroupPermissions['*']['createpage'] = false;
$wgGroupPermissions['*']['read'] = true;

# Nobody signs in over the web: the hosted copy is plain HTTP, and a fixed snapshot needs no
# accounts. Any change goes through a maintenance script on the server.
$wgEnableBotPasswords = false;
$wgAPIModules['login'] = 'ApiDisabled';
$wgAPIModules['clientlogin'] = 'ApiDisabled';
$wgAPIModules['createaccount'] = 'ApiDisabled';
$wgHooks['SpecialPage_initList'][] = static function ( array &$list ) {
    unset( $list['Userlogin'], $list['CreateAccount'] );
};

# Crawlers stay out (robots.txt too): the hosted copy shares a small server, and the live wiki
# is the one search engines should send people to.
$wgDefaultRobotPolicy = 'noindex,nofollow';

# Disable uploads (images imported via maintenance script)
$wgEnableUploads = false;

# Image rendering
$wgUseImageMagick = true;
$wgImageMagickConvertCommand = '/usr/bin/convert';
$wgGenerateThumbnailOnParse = true;

# Search
$wgSearchType = null; // MySQL fulltext

# Remove rate limits for local API access
$wgRateLimits = [];

# Increase limits for import
ini_set( 'memory_limit', '512M' );
set_time_limit( 0 );
$wgMaxArticleSize = 4096;
