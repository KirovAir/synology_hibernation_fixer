#include <stdlib.h>

int* SLIBCSzListAlloc(int size) {
    // Declare variables
    long allocatedSize;
    int baseSize;
    int* ptr;

    // Set a default base size
    baseSize = 0x200;

    // If the requested size is greater than 0x1ff, use it as the base size
    if (size > 0x1ff) {
        baseSize = size;
    }

    // Allocate memory for the list using the determined base size
    ptr = (int*)malloc((long)baseSize);

    // Check if memory allocation was successful
    if (ptr == NULL) {
        // Handle error if allocation failed
        SLIBCErrSetEx(0x200, "list_alloc.c", 0xf);
    } else {
        // Initialize the allocated memory

        // Set the first element of the list to store the base size
        *ptr = baseSize;

        // Set the second element to 0 (initially no elements in the list)
        ptr[1] = 0;

        // Calculate the address of the end of the allocated memory
        allocatedSize = (long)ptr + (long)baseSize;

        // Set some initial values for the list management
        ptr[3] = 0;
        ptr[2] = baseSize - 0x20;
        *(long*)(ptr + 4) = allocatedSize;
        *(long*)(ptr + 6) = allocatedSize - 1;

        // Set the last byte of the allocated memory to 0
        *((char*)(allocatedSize - 1)) = 0;
    }

    // Return the pointer to the allocated memory
    return ptr;
}


// Function to push a string onto a list
undefined [16] SLIBCSzListPush(long* list, char* newString, undefined8 param_3, char* param_4) {
    long listPtr;
    undefined result[16];

    // Check for valid parameters
    if ((list != (long *)0x0) && ((listPtr = *list) != 0) && (newString != (char *)0x0)) {
        // Call SLIBCSzListInsert to insert the new string at the end of the list
        result = SLIBCSzListInsert(list, *(uint *)(listPtr + 4), newString, param_4);
        return result;
    }

    // Handle error if parameters are invalid
    SLIBCErrSetEx(0xd00, "list_insert.c", 0x4a);
    result[8] = (long)listPtr;
    result[0] = 0xffffffff;
    return result;
}

undefined8 SLIBCSzListRemove(DiskList* list, int index) {
    undefined8 removedItem = 0;

    if (list == NULL || index < 0 || index >= list->metadata.elementCount) {
        SLIBCErrSetEx(0xd00, "list_remove.c", 0xd);
    } else {
        removedItem = *(undefined8*)(list->data + index);

        // Update metadata
        list->metadata.elementCount--;
        list->metadata.remainingSize += sizeof(int);
        
        // Shift elements after the removed one
        for (int i = index; i < list->metadata.elementCount; ++i) {
            list->data[i] = list->data[i + 1];
        }
    }

    return removedItem;
}



#include <stdlib.h>

// Function to sort items in a list
undefined8 SLIBCSzListSortItems(long param_1, uint param_2, undefined8 param_3) {
    // Check if the list is empty
    if (param_1 == 0) {
        return param_3;
    }

    // Check if the list size is zero
    if (*(int *)(param_1 + 0xc) == 0) {
        // Check sorting options
        if ((param_2 & 1) == 0) {
            // Case-insensitive sorting
            if ((param_2 & 0x10) == 0) {
                goto LAB_00115634;
            }
            // Clean the list and set the comparison function for case-insensitive sorting
            SLIBCSzListClean(param_1);
            int size = *(int *)(param_1 + 4);
            code *__compar = SLIBCSzListUTF8StrCasecmp;
        } else {
            // Case-sensitive sorting
            SLIBCSzListClean(param_1);
            int size = *(int *)(param_1 + 4);
            code *__compar = SLIBCSzListStrcmp;
        }

        // Use qsort for sorting
        qsort((void *)(param_1 + 0x20), (long)size, 8, __compar);
    }

LAB_00115634:
    // Set the sorting option in the list
    *(uint *)(param_1 + 0xc) = param_2;
    return param_3;
}

void SLIBCSzListClean(long param_1) {
    int listSize = *(int *)(param_1 + 4);
    int newCount = 0;

    // Iterate through the list
    for (long i = 0; i < listSize; i++) {
        long currentItem = *(long *)(param_1 + 0x20 + i * 8);

        // Check if the item is not equal to the specified value
        if (currentItem != *(long *)(param_1 + 0x18)) {
            // If the current and new indices differ, update the list
            if (newCount != (int)i) {
                *(long *)(param_1 + 0x20 + (long)newCount * 8) = currentItem;
            }
            newCount++;
        }
    }

    // Calculate the new list size and update the count and size
    int oldCount = 0;
    if (-1 < listSize) {
        oldCount = listSize;
    }
    *(int *)(param_1 + 8) = *(int *)(param_1 + 8) + (oldCount - newCount) * 8;
    *(int *)(param_1 + 4) = listSize - (oldCount - newCount);
}

void SLIBCSzListFree(void *param_1) {
    // Check if the list is not null
    if (param_1 != (void *)0x0) {
        // Free the allocated memory
        free(param_1);
    }
    // No need to return, the function is void
}


// Function to find the index of a string in a sorted list
uint SLIBCSzListFindIndex(long list, uint endIndex, uint startIndex, char* targetStr, int caseSensitive) {
    char* currentString;
    int comparisonResult;

    if (endIndex != 0xffffffff) {
        while ((int)startIndex <= (int)endIndex) {
            uint midIndex = (startIndex + endIndex) / 2;
            currentString = *(char **)(list + 0x20 + (long)(int)midIndex * 8);

            if (caseSensitive == 0) {
                comparisonResult = strcmp(targetStr, currentString);
            } else {
                comparisonResult = SLIBCUnicodeUTF8StrCaseCmp(targetStr, currentString);
                if (comparisonResult == -2) {
                    break;
                }
            }

            if (comparisonResult < 0) {
                endIndex = midIndex - 1;
            } else {
                if (comparisonResult == 0) {
                    return midIndex;
                }
                startIndex = midIndex + 1;
            }
        }
        endIndex = ~startIndex;
    }
    return endIndex;
}

// Function to find a string in a list
undefined [16] SLIBCSzListFind(long diskList, char* searchString, undefined8 param_3, undefined8 param_4) {
    char* currentString;
    uint findIndexResult;
    int stringComparisonResult;
    ulong index;
    undefined result[16];
    int listCount;

    // Check for invalid parameters or empty list
    if ((diskList == 0) || (searchString == (char *)0x0) || (*(int *)(diskList + 0xc) == 0x10)) {
        SLIBCErrSetEx(0xd00, "list_find.c", 0xf);
    } else {
        index = 0;
        listCount = *(int *)(diskList + 4);

        // If the list is sorted, use binary search
        if (*(int *)(diskList + 0xc) == 1) {
            findIndexResult = SLIBCSzListFindIndex(diskList, listCount - 1, 0, searchString, 0);
            index = (ulong)findIndexResult;
            if ((int)findIndexResult < 0) {
                index = 0xffffffff;
            }
            goto returnFunc;
        }

        // Linear search for an unsorted list
        for (; (int)index < listCount; index = index + 1) {
            currentString = *(char **)(diskList + 0x20 + index * 8);
            if ((currentString != *(char **)(diskList + 0x18)) &&
                (stringComparisonResult = strcmp(currentString, searchString), stringComparisonResult == 0)) {
                index = index & 0xffffffff;
                goto returnFunc;
            }
        }
    }
    index = 0xffffffff;

returnFunc:
    result[8] = param_4;
    result[0] = index;
    return result;
}

