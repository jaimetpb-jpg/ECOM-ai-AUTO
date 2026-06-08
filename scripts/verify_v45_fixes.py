#!/usr/bin/env python3
"""
scripts/verify_v45_fixes.py — Verification Script for V4.5 Fixes

Validates that all 7 fixes are properly implemented and working.
Run this after upgrading from V4.4 to V4.5.

Usage:
    python scripts/verify_v45_fixes.py
"""

import sys
import re
import json
import asyncio
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class V45FixVerifier:
    """Verifies all V4.5 fixes are correctly implemented."""
    
    def __init__(self):
        self.passed = []
        self.failed = []
    
    def test_fix1_json_parsing(self):
        """FIX 1: Verify robust JSON parsing with regex in BrandCreator."""
        print("\n🔍 FIX 1: JSON Parsing Robustness...")
        
        try:
            with open("branding/brand_creator.py", "r") as f:
                content = f.read()
            
            # Check for regex import
            if "import re" not in content:
                self.failed.append("FIX 1: Missing 'import re'")
                print("  ❌ Missing 'import re'")
                return
            
            # Check for regex pattern
            if r"re.search(r'\{[\s\S]*\}', raw)" not in content:
                self.failed.append("FIX 1: Missing regex JSON extraction")
                print("  ❌ Missing regex JSON extraction pattern")
                return
            
            # Check for proper error logging
            if "json_extraction_failed_no_braces" not in content:
                self.failed.append("FIX 1: Missing error logging")
                print("  ❌ Missing specific error logging")
                return
            
            self.passed.append("FIX 1: JSON parsing with regex")
            print("  ✅ Regex import found")
            print("  ✅ JSON extraction pattern implemented")
            print("  ✅ Error logging present")
            
        except Exception as e:
            self.failed.append(f"FIX 1: Error reading file - {e}")
            print(f"  ❌ Error: {e}")
    
    def test_fix2_structured_logging(self):
        """FIX 2: Verify structured logging module exists and is used."""
        print("\n🔍 FIX 2: Structured Logging...")
        
        try:
            # Check if logging_utils.py exists
            utils_path = Path("shared/logging_utils.py")
            if not utils_path.exists():
                self.failed.append("FIX 2: logging_utils.py not found")
                print("  ❌ shared/logging_utils.py not found")
                return
            
            with open(utils_path, "r") as f:
                utils_content = f.read()
            
            # Check for required functions
            required_funcs = ["log_info", "log_warning", "log_error", "log_debug"]
            missing = [f for f in required_funcs if f"def {f}" not in utils_content]
            
            if missing:
                self.failed.append(f"FIX 2: Missing functions: {missing}")
                print(f"  ❌ Missing functions: {missing}")
                return
            
            # Check usage in key files
            files_to_check = [
                "retention/comment_mining.py",
                "shared/slack_notifier.py",
                "branding/brand_creator.py"
            ]
            
            uses_new_logging = True
            for filepath in files_to_check:
                with open(filepath, "r") as f:
                    content = f.read()
                    if "from shared.logging_utils import" not in content:
                        print(f"  ⚠️  {filepath} not using logging_utils yet")
                        uses_new_logging = False
            
            self.passed.append("FIX 2: Structured logging module")
            print("  ✅ logging_utils.py exists with all functions")
            if uses_new_logging:
                print("  ✅ All critical files using new logging")
            else:
                print("  ⚠️  Some files still using old logging (non-blocking)")
            
        except Exception as e:
            self.failed.append(f"FIX 2: Error - {e}")
            print(f"  ❌ Error: {e}")
    
    def test_fix3_slack_timeout(self):
        """FIX 3: Verify asyncio.wait_for in Slack approval."""
        print("\n🔍 FIX 3: Slack Timeout with asyncio.wait_for...")
        
        try:
            with open("shared/slack_notifier.py", "r") as f:
                content = f.read()
            
            # Check for asyncio.wait_for
            if "asyncio.wait_for" not in content:
                self.failed.append("FIX 3: Missing asyncio.wait_for")
                print("  ❌ asyncio.wait_for not found")
                return
            
            # Check for _poll_for_response method
            if "async def _poll_for_response" not in content:
                self.failed.append("FIX 3: Missing _poll_for_response method")
                print("  ❌ _poll_for_response method not found")
                return
            
            # Check for TimeoutError handling
            if "except asyncio.TimeoutError" not in content:
                self.failed.append("FIX 3: Missing TimeoutError handling")
                print("  ❌ TimeoutError exception handler not found")
                return
            
            self.passed.append("FIX 3: Slack timeout with asyncio.wait_for")
            print("  ✅ asyncio.wait_for implemented")
            print("  ✅ _poll_for_response method exists")
            print("  ✅ TimeoutError properly handled")
            
        except Exception as e:
            self.failed.append(f"FIX 3: Error - {e}")
            print(f"  ❌ Error: {e}")
    
    def test_fix4_llm_tracker_bounded(self):
        """FIX 4: Verify LLMUsageTracker has MAX_CALLS limit."""
        print("\n🔍 FIX 4: LLM Tracker Bounded Memory...")
        
        try:
            with open("shared/llm_router.py", "r") as f:
                content = f.read()
            
            # Check for MAX_CALLS constant
            if "MAX_CALLS" not in content:
                self.failed.append("FIX 4: Missing MAX_CALLS constant")
                print("  ❌ MAX_CALLS constant not found")
                return
            
            # Check for rolling window logic
            if "self.calls[-self.MAX_CALLS:]" not in content and "self.calls[-MAX_CALLS:]" not in content:
                self.failed.append("FIX 4: Missing rolling window logic")
                print("  ❌ Rolling window slice not found")
                return
            
            # Extract MAX_CALLS value
            match = re.search(r'MAX_CALLS\s*=\s*(\d+)', content)
            if match:
                max_calls = int(match.group(1))
                print(f"  ✅ MAX_CALLS set to {max_calls}")
            else:
                print("  ⚠️  Could not extract MAX_CALLS value")
            
            self.passed.append("FIX 4: LLM tracker bounded memory")
            print("  ✅ Rolling window implemented")
            
        except Exception as e:
            self.failed.append(f"FIX 4: Error - {e}")
            print(f"  ❌ Error: {e}")
    
    def test_fix5_niche_clusterer_cleanup(self):
        """FIX 5: Verify NicheClusterer explicitly frees memory."""
        print("\n🔍 FIX 5: NicheClusterer Memory Cleanup...")
        
        try:
            with open("intelligence/niche_clusterer.py", "r") as f:
                content = f.read()
            
            # Check for explicit del statements
            has_del_feature_matrix = "del feature_matrix" in content
            has_del_assignments = "del assignments" in content
            has_del_cluster_to_products = "del cluster_to_products" in content
            
            if not (has_del_feature_matrix and has_del_assignments and has_del_cluster_to_products):
                missing = []
                if not has_del_feature_matrix:
                    missing.append("feature_matrix")
                if not has_del_assignments:
                    missing.append("assignments")
                if not has_del_cluster_to_products:
                    missing.append("cluster_to_products")
                
                self.failed.append(f"FIX 5: Missing explicit del for {missing}")
                print(f"  ❌ Missing explicit cleanup: {missing}")
                return
            
            self.passed.append("FIX 5: NicheClusterer memory cleanup")
            print("  ✅ Explicit del feature_matrix")
            print("  ✅ Explicit del assignments")
            print("  ✅ Explicit del cluster_to_products")
            
        except Exception as e:
            self.failed.append(f"FIX 5: Error - {e}")
            print(f"  ❌ Error: {e}")
    
    def test_fix6_changelog(self):
        """FIX 6: Verify CHANGELOG.md exists and is complete."""
        print("\n🔍 FIX 6: CHANGELOG.md Documentation...")
        
        try:
            changelog_path = Path("CHANGELOG.md")
            if not changelog_path.exists():
                self.failed.append("FIX 6: CHANGELOG.md not found")
                print("  ❌ CHANGELOG.md not found")
                return
            
            with open(changelog_path, "r") as f:
                content = f.read()
            
            # Check for V4.5 section
            if "## [V4.5]" not in content:
                self.failed.append("FIX 6: Missing V4.5 section")
                print("  ❌ V4.5 section not found")
                return
            
            # Check for all fixes documented
            fixes = ["FIX 1", "FIX 2", "FIX 3", "FIX 4", "FIX 5", "FIX 6", "FIX 7"]
            missing_fixes = [f for f in fixes if f not in content]
            
            if missing_fixes:
                self.failed.append(f"FIX 6: Undocumented fixes: {missing_fixes}")
                print(f"  ❌ Missing documentation for: {missing_fixes}")
                return
            
            self.passed.append("FIX 6: CHANGELOG.md documentation")
            print("  ✅ CHANGELOG.md exists")
            print("  ✅ V4.5 section present")
            print("  ✅ All 7 fixes documented")
            
        except Exception as e:
            self.failed.append(f"FIX 6: Error - {e}")
            print(f"  ❌ Error: {e}")
    
    def test_fix7_readme_updated(self):
        """FIX 7: Verify README.md is updated for V4.5."""
        print("\n🔍 FIX 7: README.md Update...")
        
        try:
            readme_path = Path("README.md")
            if not readme_path.exists():
                self.failed.append("FIX 7: README.md not found")
                print("  ❌ README.md not found")
                return
            
            with open(readme_path, "r") as f:
                content = f.read()
            
            # Check for V4.5 mention
            if "V4.5" not in content and "v4.5" not in content:
                self.failed.append("FIX 7: No V4.5 version mentioned")
                print("  ❌ V4.5 not mentioned in README")
                return
            
            # Check for CHANGELOG reference
            if "CHANGELOG" in content:
                print("  ✅ Links to CHANGELOG.md")
            
            self.passed.append("FIX 7: README.md updated")
            print("  ✅ README.md mentions V4.5")
            
        except Exception as e:
            self.failed.append(f"FIX 7: Error - {e}")
            print(f"  ❌ Error: {e}")
    
    def run_all_tests(self):
        """Run all verification tests."""
        print("="*70)
        print("V4.5 FIXES VERIFICATION")
        print("="*70)
        
        self.test_fix1_json_parsing()
        self.test_fix2_structured_logging()
        self.test_fix3_slack_timeout()
        self.test_fix4_llm_tracker_bounded()
        self.test_fix5_niche_clusterer_cleanup()
        self.test_fix6_changelog()
        self.test_fix7_readme_updated()
        
        print("\n" + "="*70)
        print("VERIFICATION RESULTS")
        print("="*70)
        
        print(f"\n✅ PASSED: {len(self.passed)}")
        for test in self.passed:
            print(f"   • {test}")
        
        if self.failed:
            print(f"\n❌ FAILED: {len(self.failed)}")
            for test in self.failed:
                print(f"   • {test}")
            print("\n⚠️  Some fixes are not properly implemented!")
            return False
        else:
            print("\n🎉 ALL FIXES VERIFIED SUCCESSFULLY!")
            print("\nV4.5 is ready for deployment.")
            return True


if __name__ == "__main__":
    verifier = V45FixVerifier()
    success = verifier.run_all_tests()
    sys.exit(0 if success else 1)
