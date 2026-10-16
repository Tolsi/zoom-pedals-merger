#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <objc/runtime.h>

__attribute__((constructor))
static void fix_alert_button(void) {
    // Add missing setTextColor: to _NSAlertButton (removed in newer macOS)
    Class cls = NSClassFromString(@"_NSAlertButton");
    if (cls && !class_getInstanceMethod(cls, @selector(setTextColor:))) {
        class_addMethod(cls, @selector(setTextColor:),
            imp_implementationWithBlock(^(id self, id color) {
                // no-op: silently ignore
            }),
            "v@:@");
    }
}
