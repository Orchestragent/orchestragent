#!/usr/bin/env python3
"""Generate the dependency-free Xcode project using only Python's standard library."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def uid(name):
    return hashlib.sha256(name.encode()).hexdigest()[:24].upper()

def quoted(value):
    return json.dumps(str(value))

objects = []
def obj(name, body):
    key = uid(name)
    objects.append(f"\t\t{key} = {{ {body} }};")
    return key

def setting(values):
    return " ".join(f"{key} = {quoted(value)};" for key, value in values.items())

sources = sorted((ROOT / "Sources").glob("*.swift"))
tests = sorted((ROOT / "Tests").glob("*.swift"))
resources = [ROOT / "Resources/Assets.xcassets", ROOT / "Resources/Localizable.xcstrings"]
source_refs, source_builds, test_refs, test_builds, resource_refs, resource_builds = [], [], [], [], [], []
for group, files, refs, builds in [("src", sources, source_refs, source_builds), ("test", tests, test_refs, test_builds), ("resource", resources, resource_refs, resource_builds)]:
    for path in files:
        rel = path.relative_to(ROOT)
        file_type = "sourcecode.swift" if path.suffix == ".swift" else "folder.assetcatalog" if path.suffix == ".xcassets" else "text.json.xcstrings"
        ref = obj(f"ref:{rel}", f"isa = PBXFileReference; lastKnownFileType = {file_type}; path = {quoted(rel)}; sourceTree = SOURCE_ROOT;")
        refs.append(ref)
        builds.append(obj(f"build:{rel}", f"isa = PBXBuildFile; fileRef = {ref};"))
app = obj("app-product", 'isa = PBXFileReference; explicitFileType = wrapper.application; path = OrchStudio.app; sourceTree = BUILT_PRODUCTS_DIR;')
test = obj("test-product", 'isa = PBXFileReference; explicitFileType = wrapper.cfbundle; path = OrchStudioTests.xctest; sourceTree = BUILT_PRODUCTS_DIR;')
products = obj("products", f"isa = PBXGroup; children = ({app},{test},); name = Products; sourceTree = \"<group>\";")
main = obj("main-group", f"isa = PBXGroup; children = ({','.join(source_refs + test_refs + resource_refs + [products])},); sourceTree = \"<group>\";")
def phase(name, isa, files):
    return obj(name, f"isa = {isa}; buildActionMask = 2147483647; files = ({','.join(files)}{',' if files else ''}); runOnlyForDeploymentPostprocessing = 0;")
src_phase = phase("source-phase", "PBXSourcesBuildPhase", source_builds)
res_phase = phase("resource-phase", "PBXResourcesBuildPhase", resource_builds)
framework_phase = phase("framework-phase", "PBXFrameworksBuildPhase", [])
test_phase = phase("test-phase", "PBXSourcesBuildPhase", test_builds)
test_framework_phase = phase("test-framework-phase", "PBXFrameworksBuildPhase", [])
common = {"MACOSX_DEPLOYMENT_TARGET": "14.0", "SDKROOT": "macosx", "SWIFT_VERSION": "5.0", "CLANG_ENABLE_MODULES": "YES", "ENABLE_STRICT_OBJC_MSGSEND": "YES"}
app_settings = {"PRODUCT_NAME": "OrchStudio", "PRODUCT_BUNDLE_IDENTIFIER": "dev.orchestragent.studio", "INFOPLIST_FILE": "Resources/Info.plist", "CODE_SIGN_IDENTITY": "-", "CODE_SIGN_STYLE": "Manual", "ENABLE_APP_SANDBOX": "NO", "ENABLE_HARDENED_RUNTIME": "YES", "SWIFT_EMIT_LOC_STRINGS": "YES", "ASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME": "AccentColor", "ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon", "LD_RUNPATH_SEARCH_PATHS": "$(inherited) @executable_path/../Frameworks", "MARKETING_VERSION": "0.1.0", "CURRENT_PROJECT_VERSION": "1"}
test_settings = {"PRODUCT_NAME": "OrchStudioTests", "PRODUCT_BUNDLE_IDENTIFIER": "dev.orchestragent.studio.tests", "GENERATE_INFOPLIST_FILE": "YES", "CODE_SIGN_IDENTITY": "-", "CODE_SIGN_STYLE": "Manual", "TEST_HOST": "$(BUILT_PRODUCTS_DIR)/OrchStudio.app/Contents/MacOS/OrchStudio", "BUNDLE_LOADER": "$(TEST_HOST)", "LD_RUNPATH_SEARCH_PATHS": "$(inherited) @executable_path/../Frameworks @loader_path/../Frameworks"}
def configs(name, extra):
    keys = []
    for mode in ["Debug", "Release"]:
        values = dict(common) | extra | {"SWIFT_OPTIMIZATION_LEVEL": "-Onone" if mode == "Debug" else "-O", "DEBUG_INFORMATION_FORMAT": "dwarf" if mode == "Debug" else "dwarf-with-dsym"}
        if mode == "Debug": values |= {"ENABLE_TESTABILITY": "YES", "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "DEBUG"}
        keys.append(obj(f"{name}-{mode}", f"isa = XCBuildConfiguration; buildSettings = {{ {setting(values)} }}; name = {mode};"))
    return obj(f"{name}-configs", f"isa = XCConfigurationList; buildConfigurations = ({','.join(keys)},); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;")
project_configs = configs("project", {})
app_configs = configs("app", app_settings)
test_configs = configs("tests", test_settings)
project_id, app_id, test_id = uid("project"), uid("app-target"), uid("test-target")
proxy = obj("target-proxy", f"isa = PBXContainerItemProxy; containerPortal = {project_id}; proxyType = 1; remoteGlobalIDString = {app_id}; remoteInfo = OrchStudio;")
dep = obj("test-dependency", f"isa = PBXTargetDependency; target = {app_id}; targetProxy = {proxy};")
obj("app-target", f"isa = PBXNativeTarget; buildConfigurationList = {app_configs}; buildPhases = ({src_phase},{framework_phase},{res_phase},); buildRules = (); dependencies = (); name = OrchStudio; productName = OrchStudio; productReference = {app}; productType = \"com.apple.product-type.application\";")
obj("test-target", f"isa = PBXNativeTarget; buildConfigurationList = {test_configs}; buildPhases = ({test_phase},{test_framework_phase},); buildRules = (); dependencies = ({dep},); name = OrchStudioTests; productName = OrchStudioTests; productReference = {test}; productType = \"com.apple.product-type.bundle.unit-test\";")
obj("project", f"isa = PBXProject; attributes = {{ BuildIndependentTargetsInParallel = YES; LastUpgradeCheck = 1540; }}; buildConfigurationList = {project_configs}; compatibilityVersion = \"Xcode 14.0\"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en,\"zh-Hans\",Base,); mainGroup = {main}; productRefGroup = {products}; projectDirPath = \"\"; projectRoot = \"\"; targets = ({app_id},{test_id},);")
project = ROOT / "OrchStudio.xcodeproj"
project.mkdir(exist_ok=True)
(project / "project.pbxproj").write_text("// !$*UTF8*$!\n{\n\tarchiveVersion = 1;\n\tclasses = {};\n\tobjectVersion = 56;\n\tobjects = {\n" + "\n".join(objects) + f"\n\t}};\n\trootObject = {project_id};\n}}\n")
scheme = project / "xcshareddata/xcschemes/OrchStudio.xcscheme"
scheme.parent.mkdir(parents=True, exist_ok=True)
def reference(target, product):
    return f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target}" BuildableName="{product}" BlueprintName="{product.split(".")[0]}" ReferencedContainer="container:OrchStudio.xcodeproj"/>'
scheme.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="1540" version="1.3">
<BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">{reference(app_id, "OrchStudio.app")}</BuildActionEntry></BuildActionEntries></BuildAction>
<TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" shouldUseLaunchSchemeArgsEnv="NO"><Testables><TestableReference skipped="NO">{reference(test_id, "OrchStudioTests.xctest")}</TestableReference></Testables><EnvironmentVariables><EnvironmentVariable key="ORCH_STUDIO_TESTING" value="1" isEnabled="YES"/></EnvironmentVariables></TestAction>
<LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference(app_id, "OrchStudio.app")}</BuildableProductRunnable><CommandLineArguments><CommandLineArgument argument="--demo" isEnabled="YES"/></CommandLineArguments></LaunchAction>
<ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference(app_id, "OrchStudio.app")}</BuildableProductRunnable></ProfileAction><AnalyzeAction buildConfiguration="Debug"/><ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>
''')
print(f"Generated {project.relative_to(ROOT)} ({len(sources)} app files, {len(tests)} test files)")
